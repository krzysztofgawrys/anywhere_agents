"""Hub WS handler — multi-worker proxy with transparent project ID remapping.

Frontend sees hub-assigned project IDs. Hub resolves them to (worker_id,
worker_project_id) and routes to the correct worker. Session-bound messages
(prompt, interrupt, approve/deny) route to the active worker.
"""

import asyncio
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from src.push.manager import push_manager
from src.workers.connection import WorkerConnection
from src.workers.project_index import ProjectIndex
from src.workers.registry import WorkerInfo

logger = structlog.get_logger()

HEARTBEAT_TIMEOUT = 300

# Message types that carry project_id and need ID remapping + routing
_PROJECT_BOUND = frozenset({
    "list_sessions", "session_history", "new_session", "resume_session",
    "set_auto_approve", "list_directory", "read_file", "terminal_open",
})

# Message types that route to the active worker (session-bound, no project_id)
_SESSION_BOUND = frozenset({
    "prompt", "interrupt", "approve_tool", "deny_tool", "set_model",
    "user_input_response", "terminal_input", "terminal_resize", "terminal_close",
})

# Response types from worker that carry project_id — need remapping back
_RESPONSE_WITH_PROJECT_ID = frozenset({
    "sessions", "session_history", "directory", "file_content",
    "project_updated", "project_created",
})


async def handle_websocket(
    websocket: WebSocket,
    connection_id: str,
    *,
    device_label: str = "unknown",
    workers: list[WorkerInfo],
) -> None:
    """Proxy frontend WS to multiple worker WS connections."""
    await websocket.accept()

    project_index = ProjectIndex()
    active_worker_id: str | None = None
    worker_conns: dict[str, WorkerConnection] = {}
    any_worker_alive = True

    async def make_on_message(worker_id: str) -> Any:
        """Build a per-worker message handler."""

        async def on_worker_message(msg: dict[str, Any]) -> None:
            nonlocal active_worker_id
            msg_type = msg.get("type")

            # Push notifications — handle locally
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
            # — remap all project IDs and update the index.
            if msg_type == "projects":
                w_info = next((w for w in workers if w.id == worker_id), None)
                label = w_info.label if w_info else worker_id
                raw_projects = msg.get("payload", {}).get("projects", [])
                remapped = project_index.update_from_worker(worker_id, label, raw_projects)
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

    async def on_worker_disconnect() -> None:
        nonlocal any_worker_alive
        live = any(c.connected for c in worker_conns.values())
        if not live:
            any_worker_alive = False
            try:
                await websocket.send_json({
                    "type": "error",
                    "payload": {
                        "code": "worker_disconnected",
                        "message": "All worker connections lost",
                    },
                })
            except Exception:
                pass

    # Connect to all workers
    for w in workers:
        on_msg = await make_on_message(w.id)
        conn = WorkerConnection(
            worker_url=w.url,
            worker_secret=w.secret,
            device_label=device_label,
            on_message=on_msg,
            on_disconnect=on_worker_disconnect,
        )
        try:
            await conn.connect()
            worker_conns[w.id] = conn
        except Exception as e:
            logger.warning("worker_connect_failed", worker=w.id, error=str(e))

    if not worker_conns:
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
        try:
            resp = await conn.request(
                {"type": "list_projects", "payload": {}},
                response_type="projects",
                timeout=10.0,
            )
            project_index.update_from_worker(
                wid, label, resp.get("payload", {}).get("projects", [])
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
                    try:
                        resp = await conn.request(
                            {"type": "list_projects", "payload": {}},
                            response_type="projects",
                            timeout=10.0,
                        )
                        remapped = project_index.update_from_worker(
                            wid, label, resp.get("payload", {}).get("projects", [])
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
        for conn in worker_conns.values():
            await conn.close()
        logger.info("ws_disconnected", connection_id=connection_id)
