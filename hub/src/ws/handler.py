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
from src.workers.project_index import ProjectIndex
from src.workers.registry import WorkerInfo, load_workers

logger = structlog.get_logger()

HEARTBEAT_TIMEOUT = 300

# How often to retry a worker that failed to connect (or disconnected).
# Per-WS background tasks; cancelled when the frontend WS disconnects.
RETRY_INTERVAL_S = 30.0

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

    async def make_on_message(worker_id: str) -> Any:
        """Build a per-worker message handler."""

        async def on_worker_message(msg: dict[str, Any]) -> None:
            nonlocal active_worker_id
            msg_type = msg.get("type")

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
                msg = {"type": "projects", "payload": {"projects": project_index.get_all()}}
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
            # Schedule a reconnect unless one is already in flight. Look up
            # current config from `workers` (the snapshot for this WS session) -
            # if the entry was removed from workers.json between WS connects,
            # there's nothing to reconnect to anyway.
            w = next((w for w in workers if w.id == worker_id), None)
            if w is not None and worker_id not in retry_tasks:
                retry_tasks[worker_id] = asyncio.create_task(_retry(w))

        return on_disconnect

    async def _try_connect(w: WorkerInfo) -> bool:
        """Open one WS connection to a worker. Returns True on success."""
        conn = WorkerConnection(
            worker_url=w.url,
            worker_secret=w.secret,
            device_label=device_label,
            on_message=await make_on_message(w.id),
            on_disconnect=make_on_disconnect(w.id),
        )
        try:
            await conn.connect()
        except Exception as e:
            logger.warning("worker_connect_failed", worker=w.id, error=str(e))
            return False
        worker_conns[w.id] = conn
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
                "payload": {"projects": project_index.get_all()},
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
    # (frontend may send new_session with a cached project_id before list_projects)
    for wid, conn in worker_conns.items():
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
                for wid, conn in worker_conns.items():
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
                    "payload": {"projects": all_projects},
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
        # Cancel any in-flight retry tasks first so they don't try to push a
        # `projects` payload to an already-closed WS.
        for task in list(retry_tasks.values()):
            task.cancel()
        retry_tasks.clear()
        for conn in worker_conns.values():
            await conn.close()
        logger.info("ws_disconnected", connection_id=connection_id)
