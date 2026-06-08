"""WebSocket connection to a single worker.

Two transport flavors:

  - OutboundTransport: hub dials the worker (classic mode). Used when the
    worker runs an HTTP server reachable from the hub.
  - InboundTransport: hub holds a server-accepted WS that the worker dialed
    in (reverse mode). Used when the worker sits in a closed network and
    cannot accept inbound connections.

Both flavors expose the same minimal contract (send/recv/close/alive) so
`WorkerConnection` doesn't care which one it wraps. Public API of
`WorkerConnection` stays identical for callers in ws/handler.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Protocol
from urllib.parse import quote

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection

logger = structlog.get_logger()

OnMessageFn = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class _Transport(Protocol):
    """Minimal contract a transport must satisfy for WorkerConnection."""

    async def send_text(self, data: str) -> None: ...

    async def recv_text(self) -> str: ...

    async def close(self) -> None: ...

    @property
    def alive(self) -> bool: ...


class _OutboundTransport:
    """Hub-dialed websockets.asyncio ClientConnection wrapper."""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    async def send_text(self, data: str) -> None:
        await self._ws.send(data)

    async def recv_text(self) -> str:
        # websockets yields str|bytes - we always send str so the cast is safe
        msg = await self._ws.recv()
        return msg if isinstance(msg, str) else msg.decode("utf-8", errors="replace")

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return self._ws.protocol.state.name == "OPEN"


class _InboundTransport:
    """Server-accepted FastAPI WebSocket wrapper for reverse mode.

    The WS is already accepted (by the /worker-data endpoint that handed
    it to us). We just need to read/write text frames and close cleanly.
    """

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        # FastAPI doesn't expose a clean "is open" predicate; we track it
        # ourselves by flipping on disconnect.
        self._alive = True

    async def send_text(self, data: str) -> None:
        if not self._alive:
            return
        try:
            await self._ws.send_text(data)
        except (WebSocketDisconnect, RuntimeError):
            self._alive = False
            self._signal_done()

    async def recv_text(self) -> str:
        try:
            return await self._ws.receive_text()
        except WebSocketDisconnect:
            self._alive = False
            self._signal_done()
            raise websockets.exceptions.ConnectionClosed(None, None) from None
        except RuntimeError as e:
            # Starlette raises RuntimeError("WebSocket is not connected.")
            # when receive_text is called after the peer closed. Normalize
            # to the same exception type the outbound path raises so the
            # read loop catches one thing.
            self._alive = False
            self._signal_done()
            raise websockets.exceptions.ConnectionClosed(None, None) from e

    async def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        try:
            await self._ws.close()
        except Exception:
            pass
        self._signal_done()

    def _signal_done(self) -> None:
        # Unblock the /worker-data endpoint coroutine so FastAPI can
        # finalize the request properly. The endpoint attaches an
        # asyncio.Event to ws.state.inbound_done before handing the WS to
        # the WorkerConnection; setting it lets the endpoint return.
        # Called from any path that observes the WS becoming dead.
        done = getattr(getattr(self._ws, "state", None), "inbound_done", None)
        if done is not None:
            done.set()

    @property
    def alive(self) -> bool:
        return self._alive


class WorkerConnection:
    """Manages a WS connection to one worker for one frontend client.

    Lifecycle: create -> connect()/adopt() -> forward messages -> close().
    One instance per (frontend WS connection, worker).

    For outbound workers: caller invokes connect() which dials worker_url
    and starts the read loop. For inbound workers: caller invokes adopt(ws)
    with an already-accepted FastAPI WebSocket (handed over by the
    /worker-data endpoint after pairing with a control-channel request).
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
        self._transport: _Transport | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # Pending request/response futures keyed by a unique _req_id, value
        # (response_type, future). Workers echo the _req_id we send so we
        # correlate the response to the exact request - this fixes both two
        # concurrent same-type requests colliding AND an unsolicited push
        # (e.g. `projects` after create_project) aliasing onto an in-flight
        # list_projects. _supports_req_id flips True the first time a worker
        # echoes an id; after that a message WITHOUT _req_id is treated as
        # unsolicited and never matched to a pending request.
        self._pending: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._supports_req_id = False
        # Per-response-type lock. Only load-bearing on the OLD-worker fallback
        # path (match by response_type), where it keeps two concurrent
        # same-type requests from overwriting each other; harmless otherwise.
        self._request_locks: dict[str, asyncio.Lock] = {}

    async def connect(self) -> None:
        """Outbound mode: open WS to worker with auth header and device label."""
        headers = {}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"

        url = f"{self._url}?device_label={quote(self._device_label)}"
        ws = await websockets.connect(
            url,
            additional_headers=headers,
            # Keep the websockets-library ping/pong on the hub-to-worker leg,
            # but with a generous timeout. A half-open connection (worker host
            # crash, NAT/firewall idle drop, network partition) produces no
            # TCP RST/FIN, so recv() in _read_loop would block forever and the
            # worker would appear online indefinitely - prompts routed to it
            # would silently vanish. Pings are the only thing that surfaces
            # such a dead peer as ConnectionClosed.
            #
            # The original concern was spurious drops while the worker is busy
            # (long Claude API call, tool execution). The wide 300s ping_timeout
            # tolerates long stalls while still reclaiming a truly dead
            # connection within ~ping_interval+ping_timeout. Tune up if a real
            # long-running synchronous operation ever trips it.
            ping_interval=30,
            ping_timeout=300,
            close_timeout=5,
            # Default in websockets library is 1 MiB; any larger frame
            # silently closes the connection with code 1009. Match (or
            # exceed) uvicorn's ws_max_size on the worker side so large
            # WS messages in either direction survive.
            max_size=32 * 1024 * 1024,
        )
        self._transport = _OutboundTransport(ws)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("worker_connected", url=self._url, mode="outbound")

    async def adopt(self, ws: WebSocket) -> None:
        """Inbound mode: wrap an already-accepted server WS handed to us by
        the /worker-data endpoint after pairing with a control-channel
        open_data_channel request. The WS is already accept()ed."""
        self._transport = _InboundTransport(ws)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("worker_connected", url=self._url, mode="inbound")

    async def send(self, message: dict[str, Any]) -> None:
        """Send a message to the worker (fire-and-forget)."""
        if self._transport is None:
            return
        try:
            await self._transport.send_text(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("worker_send_failed_closed")

    async def request(
        self,
        message: dict[str, Any],
        response_type: str,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send a message and wait for a specific response type.

        Used for request/response pairs like list_projects -> projects.
        The response is intercepted before the on_message callback.
        """
        loop = asyncio.get_event_loop()
        lock = self._request_locks.setdefault(response_type, asyncio.Lock())
        async with lock:
            req_id = uuid.uuid4().hex
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[req_id] = (response_type, future)
            await self.send({**message, "_req_id": req_id})
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                self._pending.pop(req_id, None)
                raise

    async def close(self) -> None:
        """Close the worker connection and stop the reader."""
        # Cancel pending requests
        for _rt, fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._transport is not None:
            await self._transport.close()
            self._transport = None

    @property
    def connected(self) -> bool:
        return self._transport is not None and self._transport.alive

    async def _read_loop(self) -> None:
        """Read messages from the transport and dispatch via on_message."""
        assert self._transport is not None
        try:
            while True:
                raw = await self._transport.recv_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Correlate responses. Workers echo our _req_id, so we match
                # exactly. A worker that doesn't echo it (older build) falls
                # back to matching by response_type - safe because the per-type
                # lock in request() keeps at most one such request in flight
                # per type.
                req_id = msg.get("_req_id")
                if req_id is not None:
                    self._supports_req_id = True
                    entry = self._pending.pop(req_id, None)
                    if entry is not None:
                        _rt, future = entry
                        if not future.done():
                            future.set_result(msg)
                    # else: late/unknown id (request already timed out) - drop.
                    continue

                # No _req_id: unsolicited push from a req_id-capable worker, or
                # a response from an older worker that doesn't echo it.
                if not self._supports_req_id:
                    msg_type = msg.get("type")
                    match = next(
                        (rid for rid, (rt, _f) in self._pending.items() if rt == msg_type),
                        None,
                    )
                    if match is not None:
                        _rt, future = self._pending.pop(match)
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
