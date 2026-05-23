"""WebSocket client connection to a single worker."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import quote

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

logger = structlog.get_logger()

OnMessageFn = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class WorkerConnection:
    """Manages a WS connection to one worker for one frontend client.

    Lifecycle: create → connect() → forward messages → close().
    One instance per (frontend WS connection, worker).
    """

    def __init__(
        self,
        worker_url: str,
        worker_secret: str,
        device_label: str,
        on_message: OnMessageFn,
        on_disconnect: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._url = worker_url
        self._secret = worker_secret
        self._device_label = device_label
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # Request/response correlation: response_type → Future
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def connect(self) -> None:
        """Open WS to worker with auth header and device label."""
        headers = {}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"

        url = f"{self._url}?device_label={quote(self._device_label)}"
        self._ws = await websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("worker_connected", url=self._url)

    async def send(self, message: dict[str, Any]) -> None:
        """Send a message to the worker (fire-and-forget)."""
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("worker_send_failed_closed")

    async def request(
        self,
        message: dict[str, Any],
        response_type: str,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send a message and wait for a specific response type.

        Used for request/response pairs like list_projects → projects.
        The response is intercepted before the on_message callback.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[response_type] = future
        await self.send(message)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(response_type, None)
            raise

    async def close(self) -> None:
        """Close the worker connection and stop the reader."""
        # Cancel pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.protocol.state.name == "OPEN"

    async def _read_loop(self) -> None:
        """Read messages from worker and dispatch via on_message callback."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Check if this is a pending request/response
                msg_type = msg.get("type")
                if msg_type and msg_type in self._pending:
                    future = self._pending.pop(msg_type)
                    if not future.done():
                        future.set_result(msg)
                    continue

                await self._on_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("worker_connection_closed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("worker_read_error", error=str(e), exc_info=True)
        finally:
            if self._on_disconnect:
                await self._on_disconnect()
