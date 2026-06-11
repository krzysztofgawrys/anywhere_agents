"""Worker WS handler - same routing as backend, no auth layer."""

import asyncio
import json
import os
import time
import uuid
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
    upload_file,
    write_file,
)
from worker_shared.projects.service import (
    create_project,
    get_project,
    get_project_by_path,
    list_projects,
    set_auto_approve,
)
from worker_shared.sdk.prefs import apply_set_effort, apply_set_model
from worker_shared.terminal.session import TerminalSession

from src.sdk.manager import SessionManager
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


async def handle_websocket(
    websocket: WebSocket,
    connection_id: str,
    device_label: str = "unknown",
) -> None:
    """Main WS message loop - called from worker main.py."""
    await manager.accept(websocket, connection_id)

    async def send(msg: dict[str, Any]) -> None:
        # Swallow send failures so a dying WS (hub closed its end, network
        # blip) doesn't propagate an exception into the SDK stream task and
        # tear down `_consume_stream` mid-turn. The session keeps draining
        # SDK output; events go nowhere until rebind() reattaches a fresh
        # send on resume_session.
        try:
            await manager.send(connection_id, msg)
        except Exception as e:
            logger.warning("ws_send_failed", error=str(e))

    sessions = SessionManager(
        send=send,
        connection_id=connection_id,
        device_label=device_label,
    )
    terminal: TerminalSession | None = None

    # Preflight credentials check: if we have no Anthropic API key on
    # disk (no env var, no persistent bootstrap blob, no
    # ~/.claude/.credentials.json), tell the user RIGHT NOW instead of
    # waiting for them to click "New session". The frontend's auth
    # store keys the banner by worker_id, so multiple data-WS connects
    # for the same worker harmlessly overwrite each other's entry.
    #
    # We do NOT spawn the bootstrap future here - that happens lazily
    # in session.start(). If the user submits credentials via this
    # preflight banner (no live bootstrap waiting), the auth_provided
    # branch below has a fallback path that persists them directly.
    from src.sdk.session import claude_bootstrap_instructions, claude_has_credentials
    if not claude_has_credentials():
        preflight_request_id = str(uuid.uuid4())
        await send({
            "type": "auth_needed",
            "payload": {
                "worker_id": os.environ.get("WORKER_ID", "") or "worker-claude",
                "agent_type": "claude",
                "flow": "api_key",
                "request_id": preflight_request_id,
                "instructions": claude_bootstrap_instructions(),
                # 10 min expiry like an in-flight bootstrap. Preflight
                # banners that "expire" are harmless - the next connect
                # re-emits.
                "expires_at": int(time.time() + 600),
            },
        })
        logger.info("auth_preflight_emitted", request_id=preflight_request_id)

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
            # Hub tags request/response messages (list_models, list_projects)
            # with a unique _req_id; echo it on the reply so the hub correlates
            # exactly instead of matching by response type.
            req_id = message.get("_req_id")
            try:
                terminal = await _route(
                    msg_type, payload, sessions, send, connection_id, terminal, req_id
                )
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
        await sessions.stop()
        manager.disconnect(connection_id)


_EFFORTS = ["low", "medium", "high", "max"]


async def _fetch_models() -> list[dict[str, Any]]:
    """Fetch the list of available Claude models from the Anthropic API.

    Tries credentials in order:
      1. ANTHROPIC_API_KEY env var (x-api-key header)
      2. Bootstrap-persisted API key from encrypted credentials store
      3. Claude.ai OAuth token from ~/.claude/.credentials.json (Bearer)

    Falls back to an empty list on any error so the hub uses its own
    type-default fallback instead of breaking the connection.
    """
    import json as _json
    from pathlib import Path

    import httpx

    headers: dict[str, str] = {"anthropic-version": "2023-06-01"}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    else:
        # Try bootstrap-persisted API key first.
        from worker_shared.sdk.credentials_store import load_credentials
        stored = load_credentials("claude")
        if stored is not None:
            data = stored.get("data") or {}
            key = data.get("api_key") if isinstance(data, dict) else None
            if isinstance(key, str) and key:
                headers["x-api-key"] = key
        if "x-api-key" not in headers:
            # Fall back to Claude.ai OAuth token.
            creds_path = Path.home() / ".claude" / ".credentials.json"
            if creds_path.is_file():
                try:
                    creds = _json.loads(creds_path.read_text())
                    token = creds.get("claudeAiOauth", {}).get("accessToken")
                    if isinstance(token, str) and token:
                        headers["Authorization"] = f"Bearer {token}"
                except Exception:
                    pass

    if "x-api-key" not in headers and "Authorization" not in headers:
        return []

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers=headers,
            )
        if resp.status_code != 200:
            return []
        raw_models = resp.json().get("data", [])

        out: list[dict[str, Any]] = []
        for m in raw_models:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            entry: dict[str, Any] = {
                "id": mid,
                "name": m.get("display_name") or mid,
            }
            # `capabilities` may be absent or JSON null; coerce to {} so
            # membership test never raises (a null would make `in` blow up
            # and abort the whole listing).
            caps = m.get("capabilities") or {}
            if isinstance(caps, dict) and "effort" in caps:
                entry["efforts"] = _EFFORTS
                entry["default_effort"] = "high"
            out.append(entry)
        return out
    except Exception:
        return []


async def _send_error(send: Any, code: str, message: str) -> None:
    await send({"type": "error", "payload": {"code": code, "message": message}})


async def _route(
    msg_type: str | None,
    payload: dict[str, Any],
    sessions: SessionManager,
    send: Any,
    connection_id: str,
    terminal: TerminalSession | None = None,
    req_id: str | None = None,
) -> TerminalSession | None:
    """Dispatch WS messages to handlers."""

    if msg_type == "ping":
        manager.touch(connection_id)
        await send({"type": "pong", "payload": {}})
        return terminal

    # ── Bootstrap auth: resolve pending request_credentials() ─────────
    # The hub forwards `auth_provided` from the browser after the user
    # submitted the modal form. Match request_id to a pending future in
    # worker_shared.sdk.bootstrap so the blocked session.start() can
    # continue. `auth_cancel` lets the user dismiss the prompt.
    if msg_type == "auth_provided":
        from worker_shared.sdk.bootstrap import resolve_auth
        from worker_shared.sdk.credentials_store import save_credentials
        request_id = payload.get("request_id")
        credentials = payload.get("credentials") or {}
        logger.info(
            "auth_provided_received",
            request_id=request_id,
            has_credentials=isinstance(credentials, dict) and bool(credentials),
        )
        if not isinstance(request_id, str) or not isinstance(credentials, dict):
            await _send_error(
                send, "bad_request", "auth_provided requires request_id and credentials"
            )
            return terminal
        # First try the live-bootstrap path (session.start() is awaiting
        # this future). If that returns False there's no in-flight
        # bootstrap, but the user might have clicked Save on the
        # preflight banner - in that case persist the credentials
        # directly so the next session.start() picks them up.
        if resolve_auth(request_id, credentials):
            return terminal
        # Fallback: preflight save.
        api_key = credentials.get("api_key") if isinstance(credentials, dict) else None
        if isinstance(api_key, str) and api_key:
            save_credentials(agent_type="claude", flow="api_key", data={"api_key": api_key})
            os.environ["ANTHROPIC_API_KEY"] = api_key
            await send({
                "type": "auth_status",
                "payload": {
                    "worker_id": os.environ.get("WORKER_ID", "") or "worker-claude",
                    "request_id": request_id,
                    "state": "completed",
                },
            })
            logger.info("auth_preflight_persisted", request_id=request_id)
        else:
            await _send_error(
                send,
                "bad_request",
                "auth_provided fallback save requires credentials.api_key",
            )
        return terminal

    if msg_type == "auth_cancel":
        from worker_shared.sdk.bootstrap import cancel_auth
        request_id = payload.get("request_id")
        if isinstance(request_id, str):
            cancel_auth(request_id, "User cancelled the bootstrap")
        return terminal

    if msg_type == "list_models":
        models = await _fetch_models()
        resp: dict[str, Any] = {"type": "models", "payload": {"models": models}}
        if req_id:
            resp["_req_id"] = req_id
        await send(resp)
        return terminal

    if msg_type == "list_projects":
        projects = await list_projects(db)
        resp = {"type": "projects", "payload": {"projects": projects}}
        if req_id:
            resp["_req_id"] = req_id
        await send(resp)
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
        # A freshly created session has no .jsonl on disk yet (the SDK writes
        # it lazily, on/after the first prompt), so list_sessions can't see it.
        # Merge in the live in-memory session so it shows in the sidebar the
        # moment it's created, not only after the first message is flushed.
        # Once its .jsonl exists, the disk entry takes over (dedup by id).
        live = sessions.current
        if (
            live is not None
            and sessions.current_project_id == project_id
            and not any(it["id"] == live.session_id for it in items)
        ):
            items.insert(0, {
                "id": live.session_id,
                "title": None,
                "preview": None,
                "message_count": 0,
                "mtime": time.time(),
            })
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

    if msg_type == "new_session":
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        await sessions.new_session(
            cwd=project["path"],
            project_id=project_id,
            auto_approve=project["auto_approve"],
            model=payload.get("model") or None,
            effort=payload.get("effort") or None,
        )
        return terminal

    if msg_type == "resume_session":
        project_id = payload.get("project_id")
        session_id = payload.get("session_id")
        force = bool(payload.get("force"))
        if not isinstance(project_id, int) or not isinstance(session_id, str):
            await _send_error(send, "bad_request", "project_id and session_id required")
            return terminal
        project = await get_project(db, project_id)
        if not project:
            await _send_error(send, "not_found", f"project {project_id} not found")
            return terminal
        await sessions.resume_session(
            cwd=project["path"],
            session_id=session_id,
            project_id=project_id,
            force=force,
            auto_approve=project["auto_approve"],
            model=payload.get("model") or None,
            effort=payload.get("effort") or None,
        )
        return terminal

    if msg_type == "set_auto_approve":
        project_id = payload.get("project_id")
        value = bool(payload.get("auto_approve"))
        if not isinstance(project_id, int):
            await _send_error(send, "bad_request", "project_id required")
            return terminal
        await set_auto_approve(db, project_id, value)
        sessions.set_auto_approve(value)
        await send({
            "type": "project_updated",
            "payload": {"project_id": project_id, "auto_approve": value},
        })
        return terminal

    if msg_type == "set_model":
        await apply_set_model(payload, sessions, send)
        return terminal

    if msg_type == "set_effort":
        await apply_set_effort(payload, sessions, send)
        return terminal

    if msg_type == "get_context_usage":
        usage = await sessions.get_context_usage()
        if usage is not None:
            await send({"type": "context_usage", "payload": usage})
        return terminal

    if msg_type == "prompt":
        text = payload.get("text", "")
        raw_images = payload.get("images") or []
        images: list[dict[str, str]] = []
        if isinstance(raw_images, list):
            for it in raw_images:
                if not isinstance(it, dict):
                    continue
                mt = it.get("media_type")
                data = it.get("data_b64")
                if isinstance(mt, str) and isinstance(data, str):
                    images.append({"media_type": mt, "data_b64": data})
        if not text and not images:
            await _send_error(send, "empty_prompt", "Prompt text or images required")
            return terminal
        auto_once = bool(payload.get("auto_approve"))
        stream = bool(payload.get("stream"))
        await sessions.send_prompt(
            text, auto_approve_once=auto_once, images=images, stream=stream
        )
        return terminal

    if msg_type == "interrupt":
        await sessions.interrupt()
        return terminal

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
        path = payload.get("path", "") or ""
        if not isinstance(path, str) or not path:
            await _send_error(send, "bad_request", "path required")
            return terminal
        expanded = os.path.expanduser(path)
        from pathlib import Path as _Path
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

    if msg_type == "user_input_response":
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            await _send_error(send, "bad_request", "tool_use_id required")
            return terminal
        # New protocol: payload.answers is a list[str], one per question
        # (AskUserQuestion can pose multiple questions in a single tool call).
        # Legacy fallback: payload.answer is a single string (older clients).
        raw_answers = payload.get("answers")
        if isinstance(raw_answers, list):
            answers = [a if isinstance(a, str) else str(a) for a in raw_answers]
        else:
            single = payload.get("answer", "")
            if not isinstance(single, str):
                single = str(single)
            answers = [single]
        ok = sessions.resolve_user_input(tool_use_id, answers)
        if not ok:
            await _send_error(send, "no_pending", "No pending user input request with that id")
        return terminal

    if msg_type == "approve_tool":
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            await _send_error(send, "bad_request", "tool_use_id required")
            return terminal
        ok = sessions.resolve_permission(tool_use_id, allow=True)
        if not ok:
            await _send_error(send, "no_pending", "No pending request with that id")
        return terminal

    if msg_type == "deny_tool":
        tool_use_id = payload.get("tool_use_id")
        reason = payload.get("reason", "")
        if not isinstance(tool_use_id, str):
            await _send_error(send, "bad_request", "tool_use_id required")
            return terminal
        ok = sessions.resolve_permission(tool_use_id, allow=False, reason=reason)
        if not ok:
            await _send_error(send, "no_pending", "No pending request with that id")
        return terminal

    # ── Terminal messages ─────────────────────────────────────────────────────

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

    # ── Upload over WS (used by hub upload_proxy for inbound workers) ────
    # In server (outbound) mode the hub POSTs files to /internal/upload
    # directly. Inbound workers have no listening port for that, so the
    # hub opens a fresh data WS via InboundRegistry and sends the upload
    # as a single message. content_b64 is base64; max raw size is
    # bounded by uvicorn's ws_max_size (16 MiB default) minus base64 and
    # JSON wrapper overhead - about 12 MiB raw in practice.
    if msg_type == "upload":
        import base64
        project_path = payload.get("project_path")
        rel_path = payload.get("path") or ""
        filename = payload.get("filename")
        content_b64 = payload.get("content_b64")
        on_conflict = payload.get("on_conflict") or "error"
        if (
            not isinstance(project_path, str)
            or not isinstance(filename, str)
            or not isinstance(content_b64, str)
        ):
            await send({"type": "upload_error", "payload": {
                "code": "bad_request",
                "message": "project_path, filename, content_b64 required",
            }})
            return terminal
        # Validate project_path against the DB; use the DB-stored path.
        project = await get_project_by_path(db, project_path)
        if not project:
            await send({"type": "upload_error", "payload": {
                "code": "forbidden",
                "message": "unknown project path",
            }})
            return terminal
        safe_project_path: str = project["path"]
        try:
            content = base64.b64decode(content_b64, validate=True)
        except Exception:
            await send({"type": "upload_error", "payload": {
                "code": "bad_base64",
                "message": "content_b64 is not valid base64",
            }})
            return terminal
        try:
            result = upload_file(safe_project_path, rel_path, filename, content, on_conflict)
        except FileBrowserError as exc:
            await send({"type": "upload_error", "payload": {
                "code": exc.code,
                "message": exc.message,
            }})
            return terminal
        await send({"type": "upload_response", "payload": result})
        return terminal

    await _send_error(
        send, "not_implemented", f"Message type '{msg_type}' not yet implemented"
    )
    return terminal
