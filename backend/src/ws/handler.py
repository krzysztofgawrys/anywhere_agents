"""WebSocket handler — protocol routing, heartbeat, SDK session lifecycle."""

import asyncio
import json
import time
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from src.sdk.session import Session

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
    """Main WS message loop with SDK session attached."""
    await manager.accept(websocket, connection_id)

    async def send(msg: dict[str, Any]) -> None:
        await manager.send(connection_id, msg)

    # Phase 2: one session per connection, started lazily on first prompt
    session = Session(send=send)

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
            await _route(msg_type, payload, session, send, connection_id)

    except TimeoutError:
        logger.warning("ws_heartbeat_timeout", connection_id=connection_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_error", connection_id=connection_id, error=str(e), exc_info=True)
    finally:
        await session.stop()
        manager.disconnect(connection_id)


async def _route(
    msg_type: str | None,
    payload: dict[str, Any],
    session: Session,
    send: Any,
    connection_id: str,
) -> None:
    """Dispatch WS messages to handlers."""
    if msg_type == "ping":
        manager.touch(connection_id)
        await send({"type": "pong", "payload": {}})
        return

    if msg_type == "prompt":
        text = payload.get("text", "")
        if not text:
            await send({
                "type": "error",
                "payload": {"code": "empty_prompt", "message": "Prompt text is required"},
            })
            return
        # Lazy-start session on first prompt (Phase 2: hardcoded single session)
        await session.start()
        await session.send_prompt(text)
        return

    if msg_type == "interrupt":
        await session.interrupt()
        return

    # Anything else is not yet implemented (sessions, projects, locks — later phases)
    await send({
        "type": "error",
        "payload": {
            "code": "not_implemented",
            "message": f"Message type '{msg_type}' not yet implemented",
        },
    })
