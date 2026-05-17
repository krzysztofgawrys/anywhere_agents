"""WebSocket handler — protocol routing, heartbeat, session lifecycle."""

import asyncio
import json
import time
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from src.db import db
from src.projects.service import get_project, list_projects, set_auto_approve
from src.sdk.manager import SessionManager
from src.sessions.reader import get_session_messages, list_sessions

logger = structlog.get_logger()

HEARTBEAT_INTERVAL = 20  # client sends ping every 20s
HEARTBEAT_TIMEOUT = 60  # server considers client dead after 60s


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._last_ping: dict[str, float] = {}

    async def accept(self, websocket: WebSocket, connection_id: str) -> None:
        await websocket.accept()
        self._connections[connection_id] = websocket
        self._last_ping[connection_id] = time.time()
        logger.info("ws_connected", connection_id=connection_id)

    def disconnect(self, connection_id: str) -> None:
        self._connections.pop(connection_id, None)
        self._last_ping.pop(connection_id, None)
        logger.info("ws_disconnected", connection_id=connection_id)

    def touch(self, connection_id: str) -> None:
        self._last_ping[connection_id] = time.time()

    def is_alive(self, connection_id: str) -> bool:
        last = self._last_ping.get(connection_id, 0)
        return (time.time() - last) < HEARTBEAT_TIMEOUT

    async def send(self, connection_id: str, message: dict[str, Any]) -> None:
        ws = self._connections.get(connection_id)
        if ws:
            await ws.send_json(message)

    @property
    def active_connections(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


async def handle_websocket(websocket: WebSocket, connection_id: str) -> None:
    """Main WS message loop with SessionManager attached."""
    await manager.accept(websocket, connection_id)

    async def send(msg: dict[str, Any]) -> None:
        await manager.send(connection_id, msg)

    sessions = SessionManager(send=send)

    try:
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=HEARTBEAT_TIMEOUT,
            )
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await send({
                    "type": "error",
                    "payload": {"code": "invalid_json", "message": "Invalid JSON"},
                })
                continue

            msg_type = message.get("type")
            payload = message.get("payload") or {}
            await _route(msg_type, payload, sessions, send, connection_id)

    except TimeoutError:
        logger.warning("ws_heartbeat_timeout", connection_id=connection_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_error", connection_id=connection_id, error=str(e), exc_info=True)
    finally:
        await sessions.stop()
        manager.disconnect(connection_id)


async def _send_error(send: Any, code: str, message: str) -> None:
    await send({"type": "error", "payload": {"code": code, "message": message}})


async def _route(
    msg_type: str | None,
    payload: dict[str, Any],
    sessions: SessionManager,
    send: Any,
    connection_id: str,
) -> None:
    """Dispatch WS messages to handlers."""

    if msg_type == "ping":
        manager.touch(connection_id)
        await send({"type": "pong", "payload": {}})
        return

    if msg_type == "list_projects":
        projects = await list_projects(db)
        await send({"type": "projects", "payload": {"projects": projects}})
        return

    if msg_type == "list_sessions":
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return
        items = list_sessions(project["path"])
        await send({
            "type": "sessions",
            "payload": {"project_id": project_id, "sessions": items},
        })
        return

    if msg_type == "session_history":
        project_id = payload.get("project_id")
        session_id = payload.get("session_id")
        if not isinstance(project_id, int) or not isinstance(session_id, str):
            await _send_error(send, "bad_request", "project_id and session_id required")
            return
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return
        limit = payload.get("limit", 30)
        before = payload.get("before_uuid")
        history = get_session_messages(
            project["path"], session_id, limit=limit, before_uuid=before
        )
        await send({
            "type": "session_history",
            "payload": {
                "project_id": project_id,
                "session_id": session_id,
                "messages": history,
            },
        })
        return

    if msg_type == "new_session":
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return
        await sessions.new_session(cwd=project["path"])
        return

    if msg_type == "resume_session":
        project_id = payload.get("project_id")
        session_id = payload.get("session_id")
        if not isinstance(project_id, int) or not isinstance(session_id, str):
            await _send_error(send, "bad_request", "project_id and session_id required")
            return
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return
        await sessions.resume_session(cwd=project["path"], session_id=session_id)
        return

    if msg_type == "set_auto_approve":
        project_id = payload.get("project_id")
        value = bool(payload.get("auto_approve"))
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return
        await set_auto_approve(db, project_id, value)
        await send({
            "type": "project_updated",
            "payload": {"project_id": project_id, "auto_approve": value},
        })
        return

    if msg_type == "prompt":
        text = payload.get("text", "")
        if not text:
            await _send_error(send, "empty_prompt", "Prompt text is required")
            return
        await sessions.send_prompt(text)
        return

    if msg_type == "interrupt":
        await sessions.interrupt()
        return

    await _send_error(
        send, "not_implemented", f"Message type '{msg_type}' not yet implemented"
    )
