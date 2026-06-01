"""Hub WS handler - multi-worker proxy with transparent project ID remapping.

Frontend sees hub-assigned project IDs. Hub resolves them to (worker_id,
worker_project_id) and routes to the correct worker. Session-bound messages
(prompt, interrupt, approve/deny) route to the active worker.

Workers list is read fresh from workers.json on every frontend WS connection
(no hub restart needed to pick up edits). Workers that fail to connect, or
that disconnect mid-session, are retried in background every
RETRY_INTERVAL_S seconds; on (re)connect the worker's projects are fetched
and the aggregated `projects` payload is pushed to the frontend.
"""

import asyncio
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from src.push.manager import push_manager
from src.workers.connection import WorkerConnection
from src.workers.inbound import inbound_registry
from src.workers.project_index import ProjectIndex
from src.workers.registry import WorkerInfo, load_workers

logger = structlog.get_logger()

HEARTBEAT_TIMEOUT = 300

# How often to retry a worker that failed to connect (or disconnected).
# Per-WS background tasks; cancelled when the frontend WS disconnects.
RETRY_INTERVAL_S = 30.0

# Fallback model lists when a worker doesn't implement `list_models`. Used as
# the initial value of `worker_models[w.id]` so the frontend's /model
# autocomplete has something sane immediately, even before the background
# list_models query returns. Indexed by worker type from workers.json.
_DEFAULT_MODELS_BY_TYPE: dict[str, list[dict[str, str]]] = {
    "claude": [
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
    ],
}

# Message types that carry project_id and need ID remapping + routing
_PROJECT_BOUND = frozenset({
    "list_sessions", "session_history", "new_session", "resume_session",
    "set_auto_approve", "list_directory", "read_file", "write_file",
    "terminal_open",
})

# Message types that route to the active worker (session-bound, no project_id)
_SESSION_BOUND = frozenset({
    "prompt", "interrupt", "approve_tool", "deny_tool", "set_model",
    "user_input_response", "terminal_input", "terminal_resize", "terminal_close",
})

# Response types from worker that carry project_id - need remapping back
_RESPONSE_WITH_PROJECT_ID = frozenset({
    "sessions", "session_history", "directory", "file_content",
    "file_written", "project_updated", "project_created",
})


async def handle_websocket(
    websocket: WebSocket,
    connection_id: str,
    *,
    device_label: str = "unknown",
) -> None:
    """Proxy frontend WS to multiple worker WS connections.

    Reads workers.json fresh per call so config edits are picked up without a
    hub restart. Workers that fail to connect (or disconnect mid-session) are
    retried in background by per-WS asyncio tasks until success or WS close.
    """
    await websocket.accept()

    # Fresh snapshot of the worker registry for this frontend WS session.
    # Adding/removing entries in workers.json takes effect on the next
    # frontend connection - no hub restart needed.
    workers = load_workers()

    project_index = ProjectIndex()
    active_worker_id: str | None = None
    worker_conns: dict[str, WorkerConnection] = {}
    # Per-worker background retry tasks. Keyed by worker id; entry exists iff
    # a retry loop is currently waiting/attempting for that worker.
    retry_tasks: dict[str, asyncio.Task[None]] = {}
    # Set to True by the WS handler's `finally` block before closing worker
    # connections so that the stray `on_disconnect` callback fired by each
    # `conn.close()` does NOT schedule fresh retry tasks. Without this guard
    # every frontend WS shutdown leaks N orphan retry coroutines (one per
    # worker) that keep dialing the workers from the dead handler's closure -
    # over a flaky network this stacks up into a retry storm that drowns the
    # logs and produces phantom worker_conns the new handler can never reach.
    shutting_down = False
    # Cache of each worker's available models. Seeded with the type default
    # immediately on (re)connect; a background `list_models` query refreshes
    # it once the worker responds.
    worker_models: dict[str, list[dict[str, str]]] = {}

    def _workers_payload() -> list[dict[str, Any]]:
        """Serialize every known worker (connected or not) for the frontend.

        Frontend sidebar uses this to render the worker filter dropdown so
        workers with zero projects (e.g. fresh worker-copilot skeleton) are
        still visible. `connected` mirrors the live WS connection state.
        `models` is the active model list for /model autocomplete; the cache
        is seeded with type defaults immediately on connect and refreshed in
        the background once the worker responds to `list_models`.
        """
        out: list[dict[str, Any]] = []
        for w in workers:
            conn = worker_conns.get(w.id)
            out.append({
                "id": w.id,
                "label": w.label,
                "type": w.type,
                "connected": bool(conn is not None and conn.connected),
                "models": worker_models.get(w.id) or _DEFAULT_MODELS_BY_TYPE.get(w.type, []),
            })
        return out

    async def _refresh_models(w: WorkerInfo) -> None:
        """Background: ask a worker for its live model list, update cache, push.

        Best-effort - if the worker doesn't implement `list_models` the
        request times out and we keep whatever the type-default seeded the
        cache with. Short timeout to avoid blocking the workers payload.
        """
        conn = worker_conns.get(w.id)
        if conn is None or not conn.connected:
            return
        try:
            resp = await conn.request(
                {"type": "list_models", "payload": {}},
                response_type="models",
                timeout=5.0,
            )
        except Exception:
            return
        models = resp.get("payload", {}).get("models")
        if not isinstance(models, list) or not models:
            return
        # Normalize to {id, name} only - drop SDK-specific fields the frontend
        # doesn't render.
        normalized: list[dict[str, str]] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            normalized.append({"id": mid, "name": m.get("name") or mid})
        if not normalized:
            return
        worker_models[w.id] = normalized
        # Push a fresh `projects` payload so the frontend sees updated models
        # without waiting for the next list_projects round-trip.
        try:
            await websocket.send_json({
                "type": "projects",
                "payload": {
                    "projects": project_index.get_all(),
                    "workers": _workers_payload(),
                },
            })
        except Exception:
            pass

    async def make_on_message(worker_id: str) -> Any:
        """Build a per-worker message handler."""

        async def on_worker_message(msg: dict[str, Any]) -> None:
            nonlocal active_worker_id
            msg_type = msg.get("type")

            # Hub probes optional worker capabilities (currently `list_models`)
            # via background request/response. If a worker doesn't implement
            # one, it replies with `{type: "error", code: "not_implemented"}` -
            # that's a hub-internal capability miss, not a user-facing error.
            # Swallow it so the frontend doesn't pop a red banner on startup.
            if msg_type == "error":
                payload = msg.get("payload") or {}
                if payload.get("code") == "not_implemented":
                    logger.debug(
                        "worker_not_implemented",
                        worker=worker_id,
                        message=payload.get("message"),
                    )
                    return

            # Push notifications - handle locally
            if msg_type == "push_notify":
                payload = msg.get("payload", {})
                await push_manager.notify_all(
                    payload.get("title", "Claude finished"),
                    payload.get("body", ""),
                )
                return

            # Track active worker from session_started
            if msg_type == "session_started":
                active_worker_id = worker_id

            # Spontaneous "projects" list from worker (e.g. after create_project)
            # - remap all project IDs and update the index.
            if msg_type == "projects":
                w_info = next((w for w in workers if w.id == worker_id), None)
                label = w_info.label if w_info else worker_id
                wtype = w_info.type if w_info else "claude"
                raw_projects = msg.get("payload", {}).get("projects", [])
                remapped = project_index.update_from_worker(worker_id, label, raw_projects, wtype)
                msg = {
                    "type": "projects",
                    "payload": {
                        "projects": project_index.get_all(),
                        "workers": _workers_payload(),
                    },
                }
                try:
                    await websocket.send_json(msg)
                except Exception:
                    pass
                return

            # Remap project_id in responses back to hub IDs
            if msg_type in _RESPONSE_WITH_PROJECT_ID:
                payload = msg.get("payload", {})
                if "project_id" in payload:
                    hub_id = project_index.to_hub_id(worker_id, payload["project_id"])
                    payload["project_id"] = hub_id

            # project_created carries a nested project dict
            if msg_type == "project_created":
                payload = msg.get("payload", {})
                proj = payload.get("project")
                if proj and "id" in proj:
                    proj["id"] = project_index.to_hub_id(worker_id, proj["id"])

            # Forward to frontend
            try:
                await websocket.send_json(msg)
            except Exception:
                pass

        return on_worker_message

    def make_on_disconnect(worker_id: str) -> Any:
        """Build a per-worker disconnect handler that schedules a reconnect."""

        async def on_disconnect() -> None:
            # Drop the dead conn so routing returns a clean "worker_unavailable"
            # error to the frontend if the user tries to send to it before the
            # reconnect succeeds.
            worker_conns.pop(worker_id, None)
            logger.warning("worker_disconnected", worker=worker_id)
            # Skip the reconnect entirely when the WS handler is tearing down.
            # The `finally` block closes every worker conn, each of those
            # closes fires this callback, and without this guard we'd spawn
            # one orphan retry task per worker on every frontend WS shutdown.
            if shutting_down:
                return
            # Schedule a reconnect unless one is already in flight. Look up
            # current config from `workers` (the snapshot for this WS session) -
            # if the entry was removed from workers.json between WS connects,
            # there's nothing to reconnect to anyway.
            w = next((w for w in workers if w.id == worker_id), None)
            if w is not None and worker_id not in retry_tasks:
                retry_tasks[worker_id] = asyncio.create_task(_retry(w))

        return on_disconnect

    async def _try_connect(w: WorkerInfo) -> bool:
        """Open one WS connection to a worker. Returns True on success.

        Branches on w.mode:
          - outbound: hub dials worker_url (classic).
          - inbound: hub asks InboundRegistry to open a data channel by
            sending a command over the worker's pre-registered control WS;
            adopts the resulting server-accepted WS into a WorkerConnection.

        Both paths return the same WorkerConnection wrapper so the rest of
        the handler (routing, on_disconnect, retry) is mode-agnostic.
        """
        conn = WorkerConnection(
            worker_url=w.url,
            worker_secret=w.secret,
            device_label=device_label,
            on_message=await make_on_message(w.id),
            on_disconnect=make_on_disconnect(w.id),
        )
        try:
            if w.mode == "inbound":
                # Will raise LookupError if no control channel yet (worker
                # hasn't dialed in, or its control disconnected). The
                # caller schedules a retry in that case, same as for a
                # refused TCP connect on the outbound path.
                ws = await inbound_registry.open_session(w.id)
                await conn.adopt(ws)
            else:
                await conn.connect()
        except Exception as e:
            logger.warning("worker_connect_failed", worker=w.id, mode=w.mode, error=str(e))
            return False
        worker_conns[w.id] = conn
        # Seed the model cache with the type default so the frontend has
        # something immediately; refresh in background from the worker's
        # actual list_models response (workers without that handler keep
        # the seed).
        worker_models.setdefault(w.id, list(_DEFAULT_MODELS_BY_TYPE.get(w.type, [])))
        asyncio.create_task(_refresh_models(w))
        return True

    async def _fetch_and_push_aggregate() -> None:
        """Refresh projects from every connected worker and push to frontend.

        Called after a worker (re)connects, so the frontend immediately sees
        the new worker's projects without a manual reload.
        """
        for wid in list(worker_conns.keys()):
            conn = worker_conns.get(wid)
            if conn is None or not conn.connected:
                continue
            w_info = next((w for w in workers if w.id == wid), None)
            label = w_info.label if w_info else wid
            wtype = w_info.type if w_info else "claude"
            try:
                resp = await conn.request(
                    {"type": "list_projects", "payload": {}},
                    response_type="projects",
                    timeout=10.0,
                )
                project_index.update_from_worker(
                    wid, label, resp.get("payload", {}).get("projects", []), wtype
                )
            except Exception as e:
                logger.warning("post_connect_projects_failed", worker=wid, error=str(e))
        try:
            await websocket.send_json({
                "type": "projects",
                "payload": {
                    "projects": project_index.get_all(),
                    "workers": _workers_payload(),
                },
            })
        except Exception:
            pass

    async def _retry(w: WorkerInfo) -> None:
        """Background loop: retry a worker until connected or task cancelled."""
        try:
            while True:
                await asyncio.sleep(RETRY_INTERVAL_S)
                # Could have been connected via another path - skip if so.
                existing = worker_conns.get(w.id)
                if existing is not None and existing.connected:
                    return
                logger.info("worker_retry_attempt", worker=w.id, url=w.url)
                if await _try_connect(w):
                    logger.info("worker_retry_succeeded", worker=w.id)
                    try:
                        await _fetch_and_push_aggregate()
                    except Exception as e:
                        logger.warning("retry_post_push_failed", worker=w.id, error=str(e))
                    return
        except asyncio.CancelledError:
            return
        finally:
            retry_tasks.pop(w.id, None)

    # Initial connection pass. Workers that fail get a background retry task.
    for w in workers:
        if not await _try_connect(w):
            retry_tasks[w.id] = asyncio.create_task(_retry(w))

    # All workers failed initial connect AND there were workers configured -
    # close so the frontend reconnects (its useWebSocket hook retries
    # automatically with backoff). If we kept the WS open here the frontend
    # would sit there forever waiting for a `projects` payload that never
    # arrives until a retry tick fires.
    if not worker_conns:
        for task in list(retry_tasks.values()):
            task.cancel()
        retry_tasks.clear()
        await websocket.send_json({
            "type": "error",
            "payload": {
                "code": "no_workers",
                "message": "Could not connect to any worker",
            },
        })
        await websocket.close()
        return

    # Set active to first connected worker by default
    active_worker_id = next(iter(worker_conns))

    # Pre-populate project index so project IDs resolve immediately
    # (frontend may send new_session with a cached project_id before list_projects).
    # Snapshot to list because on_disconnect callbacks (fired on await
    # conn.request below if a worker drops) mutate worker_conns mid-iteration.
    for wid, conn in list(worker_conns.items()):
        w_info = next((w for w in workers if w.id == wid), None)
        label = w_info.label if w_info else wid
        wtype = w_info.type if w_info else "claude"
        try:
            resp = await conn.request(
                {"type": "list_projects", "payload": {}},
                response_type="projects",
                timeout=10.0,
            )
            project_index.update_from_worker(
                wid, label, resp.get("payload", {}).get("projects", []), wtype
            )
        except Exception as e:
            logger.warning("prefetch_projects_failed", worker=wid, error=str(e))

    try:
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=HEARTBEAT_TIMEOUT,
            )
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "payload": {"code": "invalid_json", "message": "Invalid JSON"},
                })
                continue

            msg_type = message.get("type")
            payload = message.get("payload") or {}

            # ── Ping: handle locally ─────────────────────────────────
            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "payload": {}})
                continue

            # ── list_projects: fan out, aggregate, remap IDs ─────────
            if msg_type == "list_projects":
                all_projects: list[dict[str, Any]] = []
                # Snapshot - await conn.request below can yield to an
                # on_disconnect callback that mutates worker_conns.
                for wid, conn in list(worker_conns.items()):
                    w_info = next((w for w in workers if w.id == wid), None)
                    label = w_info.label if w_info else wid
                    wtype = w_info.type if w_info else "claude"
                    try:
                        resp = await conn.request(
                            {"type": "list_projects", "payload": {}},
                            response_type="projects",
                            timeout=10.0,
                        )
                        remapped = project_index.update_from_worker(
                            wid, label, resp.get("payload", {}).get("projects", []), wtype
                        )
                        all_projects.extend(remapped)
                    except Exception as e:
                        logger.warning("list_projects_failed", worker=wid, error=str(e))

                all_projects.sort(
                    key=lambda p: (
                        not p.get("available", True),
                        -(p.get("last_session_mtime") or 0),
                    )
                )
                await websocket.send_json({
                    "type": "projects",
                    "payload": {
                        "projects": all_projects,
                        "workers": _workers_payload(),
                    },
                })
                continue

            # ── browse_fs / create_directory / create_project: route by worker_id or active ─
            if msg_type in ("browse_fs", "create_directory", "create_project"):
                target = payload.pop("worker_id", None) or active_worker_id
                if target and target in worker_conns:
                    await worker_conns[target].send(message)
                elif active_worker_id and active_worker_id in worker_conns:
                    await worker_conns[active_worker_id].send(message)
                continue

            # ── Bootstrap auth: route by worker_id to that specific worker ──
            # `auth_provided` carries the credentials the user pasted into
            # the modal. `auth_cancel` lets the user dismiss the prompt.
            # `auth_request_oauth` asks the worker to switch the modal
            # from paste flow to OAuth device flow (copilot only - worker
            # spawns `copilot login`, scrapes the device code, emits a
            # fresh auth_needed flow=device_code that overwrites the
            # store entry; the modal re-renders into the device panel
            # automatically).
            # All unicast to the named worker - never broadcast (the
            # opposite direction, `auth_needed` / `auth_status`, IS sent
            # by the worker to the browser via the default forward path).
            if msg_type in ("auth_provided", "auth_cancel", "auth_request_oauth"):
                target = payload.get("worker_id")
                request_id = payload.get("request_id")
                logger.info(
                    "auth_route_received",
                    msg_type=msg_type,
                    target=target,
                    request_id=request_id,
                    target_connected=isinstance(target, str) and target in worker_conns,
                )
                if not isinstance(target, str) or target not in worker_conns:
                    await websocket.send_json({
                        "type": "error",
                        "payload": {
                            "code": "worker_not_connected",
                            "message": f"Worker {target!r} is not connected for auth bootstrap",
                        },
                    })
                    continue
                await worker_conns[target].send(message)
                continue

            # ── Project-bound: resolve hub_id → worker, remap, route ─
            if msg_type in _PROJECT_BOUND:
                hub_id = payload.get("project_id")
                resolved = project_index.resolve(hub_id) if isinstance(hub_id, int) else None
                if resolved is None:
                    await websocket.send_json({
                        "type": "error",
                        "payload": {"code": "unknown_project", "message": f"Unknown project {hub_id}"},
                    })
                    continue
                worker_id, worker_pid = resolved
                if worker_id not in worker_conns:
                    await websocket.send_json({
                        "type": "error",
                        "payload": {"code": "worker_unavailable", "message": f"Worker {worker_id} not connected"},
                    })
                    continue
                # Replace hub project_id with worker-side id
                payload["project_id"] = worker_pid
                # Track active worker on session start/resume
                if msg_type in ("new_session", "resume_session"):
                    active_worker_id = worker_id
                await worker_conns[worker_id].send(message)
                continue

            # ── Session-bound: route to active worker ────────────────
            if msg_type in _SESSION_BOUND:
                if active_worker_id and active_worker_id in worker_conns:
                    await worker_conns[active_worker_id].send(message)
                else:
                    await websocket.send_json({
                        "type": "error",
                        "payload": {"code": "no_active_worker", "message": "No active worker session"},
                    })
                continue

            # ── Unknown: forward to active worker as fallback ────────
            if active_worker_id and active_worker_id in worker_conns:
                await worker_conns[active_worker_id].send(message)

    except TimeoutError:
        logger.warning("ws_heartbeat_timeout", connection_id=connection_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_error", connection_id=connection_id, error=str(e), exc_info=True)
    finally:
        # Flip the shutdown flag BEFORE touching anything else. The closure
        # `make_on_disconnect` reads this to skip the retry-scheduling branch
        # when conn.close() below fires the per-worker on_disconnect callback.
        shutting_down = True
        # Cancel any in-flight retry tasks first so they don't try to push a
        # `projects` payload to an already-closed WS.
        for task in list(retry_tasks.values()):
            task.cancel()
        retry_tasks.clear()
        # Snapshot conn list - conn.close triggers on_disconnect which mutates
        # worker_conns. shutting_down guards against spawning new retry tasks
        # from those callbacks.
        for conn in list(worker_conns.values()):
            await conn.close()
        worker_conns.clear()
        logger.info("ws_disconnected", connection_id=connection_id)
