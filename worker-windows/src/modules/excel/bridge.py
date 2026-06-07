"""WebSocket bridge between MCP tool calls and Office.js add-in.

The add-in (running inside Excel) connects to this bridge over WebSocket.
When an MCP tool is called, the bridge forwards the command to the add-in,
waits for the result, and returns it.

Protocol (bridge <-> add-in):

  Request:  {"id": "<uuid>", "command": "read_range", "params": {...}}
  Response: {"id": "<uuid>", "result": {...}}
            {"id": "<uuid>", "error": "description"}

Multiple add-ins can connect (e.g. user has two Excel windows open). Commands
are dispatched to the first available add-in. Round-robin could come later.
If no add-in is connected, tool calls return an error immediately.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import structlog

logger = structlog.get_logger()

# How long to wait for an add-in to respond to a command.
COMMAND_TIMEOUT = 30.0


class AddinConnection:
    """One connected Office.js add-in instance."""

    def __init__(self, ws: Any, addin_id: str) -> None:
        self.ws = ws
        self.addin_id = addin_id
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def send_command(
        self, command: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a command to this add-in and wait for the response."""
        call_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[call_id] = future

        msg = json.dumps({"id": call_id, "command": command, "params": params})
        try:
            await self.ws.send_text(msg)
        except Exception as e:
            self._pending.pop(call_id, None)
            raise ConnectionError(f"Failed to send to add-in: {e}") from e

        try:
            result = await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT)
            return result
        except TimeoutError:
            raise TimeoutError(
                f"Add-in did not respond to '{command}' within {COMMAND_TIMEOUT}s"
            )
        finally:
            self._pending.pop(call_id, None)

    def resolve(self, call_id: str, response: dict[str, Any]) -> bool:
        """Resolve a pending command future with the add-in's response."""
        fut = self._pending.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result(response)
        return True

    def cancel_all(self, reason: str = "Add-in disconnected") -> None:
        """Cancel all pending futures (add-in disconnected)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()


class AddinBridge:
    """Manages WebSocket connections from Office.js add-ins.

    Provides execute() for MCP tools to dispatch commands to connected add-ins.
    """

    def __init__(self) -> None:
        self._addins: dict[str, AddinConnection] = {}

    @property
    def connected_count(self) -> int:
        return len(self._addins)

    @property
    def is_connected(self) -> bool:
        return self.connected_count > 0

    def register(self, ws: Any, addin_id: str | None = None) -> AddinConnection:
        """Register a new add-in WebSocket connection."""
        aid = addin_id or str(uuid.uuid4())
        conn = AddinConnection(ws, aid)
        self._addins[aid] = conn
        logger.info("addin_connected", addin_id=aid, total=self.connected_count)
        return conn

    def unregister(self, ws: Any) -> None:
        """Remove a disconnected add-in by its WebSocket reference."""
        to_remove: list[str] = []
        for aid, conn in self._addins.items():
            if conn.ws is ws:
                to_remove.append(aid)
        for aid in to_remove:
            conn = self._addins.pop(aid, None)
            if conn:
                conn.cancel_all()
                logger.info(
                    "addin_disconnected", addin_id=aid, total=self.connected_count
                )

    def unregister_by_id(self, addin_id: str) -> None:
        """Remove an add-in connection by its ID (on disconnect)."""
        conn = self._addins.pop(addin_id, None)
        if conn:
            conn.cancel_all()
            logger.info(
                "addin_disconnected", addin_id=addin_id, total=self.connected_count
            )

    async def execute(
        self, command: str, params: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        """Execute a command on the first available add-in.

        Generates a unique call_id, sends JSON to the add-in, waits for
        the response, and returns the result or raises an exception.

        Raises ConnectionError if no add-in is connected.
        Raises TimeoutError if the add-in doesn't respond in time.
        """
        if not self._addins:
            raise ConnectionError(
                "No Excel add-in connected. Open Excel and load the "
                "Claude Excel Agent add-in from the Task Pane, then retry."
            )

        # Use the first connected add-in. Future: round-robin or target
        # a specific workbook.
        conn = next(iter(self._addins.values()))
        return await conn.send_command(command, params or {})

    def handle_message(self, addin_id: str, data: dict[str, Any]) -> bool:
        """Process an incoming message from the add-in.

        Routes the response to the matching pending command future.
        Returns True if the message matched a pending call.
        """
        conn = self._addins.get(addin_id)
        if conn is None:
            return False
        call_id = data.get("id")
        if not isinstance(call_id, str):
            return False
        return conn.resolve(call_id, data)
