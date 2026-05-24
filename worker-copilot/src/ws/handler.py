"""worker-copilot WS handler.

Skeleton phase: SDK-agnostic messages (projects, files, terminal, browse_fs)
work via shared modules. SDK-specific messages (prompt, new_session,
resume_session, interrupt, set_model, approve_tool, deny_tool,
user_input_response) respond with a `system` notice that Copilot integration
is not yet implemented. Phase 4 wires in github-copilot-sdk.
"""

import asyncio
import json
import time
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from worker_shared.db import db
from worker_shared.files import (
    FileBrowserError,
    create_absolute_directory,
    list_absolute_directory,
    list_directory,
    read_file,
    write_file,
)
from worker_shared.projects.service import (
    create_project,
    get_project,
    list_projects,
    set_auto_approve,
)
from worker_shared.terminal.session import TerminalSession

from src.sessions.reader import get_session_messages, list_sessions

logger = structlog.get_logger()

HEARTBEAT_TIMEOUT = 300


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._last_ping: dict[str, float] = {}

    async def accept(self, websocket: WebSocket, connection_id: str) -> None:
        await websocket.accept()
        self._connections[connection_id] = websocket
        self._last_ping[connection_id] = time.time()

    def disconnect(self, connection_id: str) -> None:
        self._connections.pop(connection_id, None)
        self._last_ping.pop(connection_id, None)

    def touch(self, connection_id: str) -> None:
        self._last_ping[connection_id] = time.time()

    async def send(self, connection_id: str, message: dict[str, Any]) -> None:
        ws = self._connections.get(connection_id)
        if ws:
            await ws.send_json(message)

    @property
    def active_connections(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


# SDK-specific messages the skeleton cannot satisfy yet. Each gets a friendly
# "not implemented" reply so the frontend doesn't hang on a pending request.
_SDK_NOT_IMPLEMENTED = frozenset({
    "prompt",
    "new_session",
    "resume_session",
    "interrupt",
    "set_model",
    "approve_tool",
    "deny_tool",
    "user_input_response",
})


async def handle_websocket(
    websocket: WebSocket,
    connection_id: str,
    device_label: str = "unknown",
) -> None:
    """Main WS message loop - called from main.py."""
    await manager.accept(websocket, connection_id)

    async def send(msg: dict[str, Any]) -> None:
        await manager.send(connection_id, msg)

    terminal: TerminalSession | None = None

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
            try:
                terminal = await _route(msg_type, payload, send, connection_id, terminal)
            except Exception as route_err:
                logger.error("route_error", msg_type=msg_type, error=str(route_err), exc_info=True)
                await send({
                    "type": "error",
                    "payload": {"code": "internal_error", "message": str(route_err)},
                })

    except TimeoutError:
        logger.warning("ws_heartbeat_timeout", connection_id=connection_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_error", connection_id=connection_id, error=str(e), exc_info=True)
    finally:
        if terminal is not None:
            await terminal.stop()
        manager.disconnect(connection_id)


async def _send_error(send: Any, code: str, message: str) -> None:
    await send({"type": "error", "payload": {"code": code, "message": message}})


async def _route(
    msg_type: str | None,
    payload: dict[str, Any],
    send: Any,
    connection_id: str,
    terminal: TerminalSession | None = None,
) -> TerminalSession | None:
    """Dispatch WS messages to handlers.

    SDK-agnostic messages (projects, files, terminal, browse_fs) are fully
    wired through worker_shared. SDK-specific messages return a friendly stub.
    """

    if msg_type == "ping":
        manager.touch(connection_id)
        await send({"type": "pong", "payload": {}})
        return terminal

    # ── SDK-agnostic: projects ─────────────────────────────────────────────
    if msg_type == "list_projects":
        projects = await list_projects(db)
        await send({"type": "projects", "payload": {"projects": projects}})
        return terminal

    if msg_type == "list_sessions":
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        items = list_sessions(project["path"])
        await send({
            "type": "sessions",
            "payload": {"project_id": project_id, "sessions": items},
        })
        return terminal

    if msg_type == "session_history":
        project_id = payload.get("project_id")
        session_id = payload.get("session_id")
        if not isinstance(project_id, int) or not isinstance(session_id, str):
            await _send_error(send, "bad_request", "project_id and session_id required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        limit = payload.get("limit", 30)
        before = payload.get("before_uuid")
        result = get_session_messages(
            project["path"], session_id, limit=limit, before_uuid=before
        )
        await send({
            "type": "session_history",
            "payload": {
                "project_id": project_id,
                "session_id": session_id,
                "messages": result["messages"],
                "has_more": result["has_more"],
                "oldest_uuid": result["oldest_uuid"],
                "before_uuid": before,
            },
        })
        return terminal

    if msg_type == "set_auto_approve":
        project_id = payload.get("project_id")
        value = bool(payload.get("auto_approve"))
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return terminal
        await set_auto_approve(db, project_id, value)
        await send({
            "type": "project_updated",
            "payload": {"project_id": project_id, "auto_approve": value},
        })
        return terminal

    # ── SDK-agnostic: files ────────────────────────────────────────────────
    if msg_type == "list_directory":
        project_id = payload.get("project_id")
        rel_path = payload.get("path", "") or ""
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return terminal
        if not isinstance(rel_path, str):
            await _send_error(send, "bad_request", "path must be a string")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        try:
            result = list_directory(project["path"], rel_path)
        except FileBrowserError as exc:
            await _send_error(send, exc.code, exc.message)
            return terminal
        await send({
            "type": "directory",
            "payload": {
                "project_id": project_id,
                "root": project["path"],
                "path": result["path"],
                "parent": result["parent"],
                "entries": result["entries"],
            },
        })
        return terminal

    if msg_type == "read_file":
        project_id = payload.get("project_id")
        rel_path = payload.get("path")
        if not isinstance(project_id, int) or not isinstance(rel_path, str):
            await _send_error(send, "bad_request", "project_id and path required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        try:
            result = read_file(project["path"], rel_path)
        except FileBrowserError as exc:
            await _send_error(send, exc.code, exc.message)
            return terminal
        await send({
            "type": "file_content",
            "payload": {
                "project_id": project_id,
                "path": result["path"],
                "size": result["size"],
                "too_large": result["too_large"],
                "encoding": result["encoding"],
                "content": result["content"],
            },
        })
        return terminal

    if msg_type == "write_file":
        project_id = payload.get("project_id")
        rel_path = payload.get("path")
        content = payload.get("content")
        if not isinstance(project_id, int) or not isinstance(rel_path, str):
            await _send_error(send, "bad_request", "project_id and path required")
            return terminal
        if not isinstance(content, str):
            await _send_error(send, "bad_request", "content (string) required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        try:
            result = write_file(project["path"], rel_path, content)
        except FileBrowserError as exc:
            await _send_error(send, exc.code, exc.message)
            return terminal
        await send({
            "type": "file_written",
            "payload": {
                "project_id": project_id,
                "path": result["path"],
                "size": result["size"],
            },
        })
        return terminal

    if msg_type == "browse_fs":
        import os
        path = payload.get("path", "") or ""
        if not isinstance(path, str):
            await _send_error(send, "bad_request", "path must be a string")
            return terminal
        if not path:
            path = os.path.expanduser("~")
        try:
            result = list_absolute_directory(path)
        except FileBrowserError as exc:
            await _send_error(send, exc.code, exc.message)
            return terminal
        await send({
            "type": "fs_directory",
            "payload": {
                "path": result["path"],
                "parent": result["parent"],
                "entries": result["entries"],
            },
        })
        return terminal

    if msg_type == "create_directory":
        path = payload.get("path", "") or ""
        if not isinstance(path, str) or not path:
            await _send_error(send, "bad_request", "path required")
            return terminal
        try:
            create_absolute_directory(path)
            result = list_absolute_directory(path)
        except FileBrowserError as exc:
            await _send_error(send, exc.code, exc.message)
            return terminal
        await send({
            "type": "fs_directory",
            "payload": {
                "path": result["path"],
                "parent": result["parent"],
                "entries": result["entries"],
            },
        })
        return terminal

    if msg_type == "create_project":
        import os
        from pathlib import Path as _Path
        path = payload.get("path", "") or ""
        if not isinstance(path, str) or not path:
            await _send_error(send, "bad_request", "path required")
            return terminal
        expanded = os.path.expanduser(path)
        target = _Path(expanded).resolve()
        if not target.exists() or not target.is_dir():
            await _send_error(send, "not_found", "Directory not found or is not a directory")
            return terminal
        project = await create_project(db, str(target))
        projects = await list_projects(db)
        await send({
            "type": "project_created",
            "payload": {"project": project},
        })
        await send({"type": "projects", "payload": {"projects": projects}})
        return terminal

    # ── SDK-agnostic: terminal ─────────────────────────────────────────────
    if msg_type == "terminal_open":
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        if terminal is not None:
            await terminal.stop()
        cols = int(payload.get("cols") or 80)
        rows = int(payload.get("rows") or 24)
        terminal = TerminalSession(send=send, cwd=project["path"])
        await terminal.start(cols=cols, rows=rows)
        await send({"type": "terminal_ready", "payload": {}})
        return terminal

    if msg_type == "terminal_input":
        if terminal is None:
            await _send_error(send, "no_terminal", "No terminal open")
            return terminal
        data = payload.get("data", "")
        if isinstance(data, str):
            terminal.write(data)
        return terminal

    if msg_type == "terminal_resize":
        if terminal is not None:
            cols = int(payload.get("cols") or 80)
            rows = int(payload.get("rows") or 24)
            terminal.resize(cols, rows)
        return terminal

    if msg_type == "terminal_close":
        if terminal is not None:
            await terminal.stop()
            terminal = None
        return terminal

    # ── SDK-specific: not yet implemented in this skeleton ─────────────────
    if msg_type in _SDK_NOT_IMPLEMENTED:
        await send({
            "type": "system",
            "payload": {
                "session_id": "",
                "subtype": "copilot_not_implemented",
                "data": {
                    "message_type": msg_type,
                    "message": (
                        "worker-copilot skeleton: github-copilot-sdk integration is "
                        "wired up but not implemented yet. This message type "
                        "(" + str(msg_type) + ") will work after phase 4."
                    ),
                },
            },
        })
        return terminal

    await _send_error(
        send, "not_implemented", f"Message type '{msg_type}' not yet implemented"
    )
    return terminal
