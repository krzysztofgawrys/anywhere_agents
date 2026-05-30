"""Reverse-mode hub dialer for worker-copilot.

Two-channel model:

  - Control channel (persistent): worker dials hub at /worker-register on
    startup, sends a register handshake identifying itself, and keeps
    the WS open. The hub sends `open_data_channel` commands over this WS
    every time a browser needs to talk to this worker.

  - Data channels (on demand, short-lived): per `open_data_channel` the
    worker dials /worker-data?worker_id=...&session_id=... - one fresh
    WS per browser session. The worker then hands the WS to the
    existing `handle_websocket()` function via a thin adapter, which
    means all routing/session/SDK code is reused without changes.

Why control + data instead of one multiplexed connection:
- Worker's `handle_websocket()` is single-session-per-WS by design.
  Splitting per session keeps the worker mental model identical to
  outbound mode (where every browser connect = new hub-dialed WS).
- Failure isolation: if the control WS dies, active data WS keep
  running. Worker reconnects control without disturbing live sessions.

Auth: shared WORKER_SECRET in Authorization header. When the hub is
behind Cloudflare Access, CF-Access-Client-Id + CF-Access-Client-Secret
are added so CF Access service token gates both endpoints at the edge.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
import websockets
from fastapi import WebSocketDisconnect

from src.ws.handler import handle_websocket

logger = structlog.get_logger()


class _ClientWSAdapter:
    """Quacks like fastapi.WebSocket for the subset handle_websocket uses.

    The existing handler was written against FastAPI's server-side
    WebSocket. The dialer holds a websockets.ClientConnection. Rather than
    fork handle_websocket, we wrap the client connection in an object that
    exposes the same minimal surface: accept (no-op), receive_text,
    send_json/send_text, close, and a couple of stub properties.

    `WebSocketDisconnect` is raised from receive_text on peer close to
    match the exception the original handler catches.
    """

    def __init__(self, ws: Any) -> None:
        # `ws` is a websockets client connection (the type name varies
        # across websockets library versions: 13.x had ClientConnection,
        # older had WebSocketClientProtocol). Typed as Any so the dialer
        # imports cleanly under any installed version.
        self._ws = ws
        self._accepted = False

    async def accept(self) -> None:
        # Already connected as a client - nothing to accept on this end.
        self._accepted = True

    async def receive_text(self) -> str:
        try:
            raw = await self._ws.recv()
        except websockets.exceptions.ConnectionClosed as exc:
            raise WebSocketDisconnect(code=exc.code or 1000) from None
        return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")

    async def send_text(self, data: str) -> None:
        await self._ws.send(data)

    async def send_json(self, data: Any) -> None:
        await self._ws.send(json.dumps(data))

    async def close(self, code: int = 1000) -> None:
        try:
            await self._ws.close(code=code)
        except Exception:
            pass

    # Handler reads these on the *worker side* main.py auth gate, not
    # inside handle_websocket itself. Stubs included for safety in case
    # something downstream peeks at them.
    @property
    def headers(self) -> dict[str, str]:
        return {}

    @property
    def query_params(self) -> dict[str, str]:
        return {}

    @property
    def client(self) -> None:
        return None


class _Backoff:
    """Exponential backoff with cap. Reset on a successful pass."""

    def __init__(self, start: float = 1.0, cap: float = 30.0, multiplier: float = 2.0) -> None:
        self._start = start
        self._cap = cap
        self._multiplier = multiplier
        self._current = start

    def next(self) -> float:
        wait = self._current
        self._current = min(self._current * self._multiplier, self._cap)
        return wait

    def reset(self) -> None:
        self._current = self._start


class HubDialer:
    """Owns the control WS to the hub and spawns per-session data WS.

    Lifecycle: start() launches the run loop in a background task; stop()
    cancels everything (control WS + any live data sessions). Designed to
    sit inside a FastAPI lifespan.
    """

    def __init__(
        self,
        hub_url: str,
        worker_id: str,
        worker_secret: str,
        worker_type: str = "copilot",
        worker_label: str = "",
        cf_client_id: str | None = None,
        cf_client_secret: str | None = None,
    ) -> None:
        self._hub_url = hub_url.rstrip("/")
        self._worker_id = worker_id
        self._worker_secret = worker_secret
        self._worker_type = worker_type
        self._worker_label = worker_label or worker_id
        self._cf_client_id = cf_client_id
        self._cf_client_secret = cf_client_secret
        self._data_tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None

    # ── Public lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._run_task is not None:
            return
        self._run_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
            self._run_task = None
        # Cancel any in-flight data sessions. They'll log a disconnect and
        # tear down their SDK sessions via existing handle_websocket
        # `finally` blocks.
        for task in list(self._data_tasks):
            task.cancel()
        if self._data_tasks:
            await asyncio.gather(*self._data_tasks, return_exceptions=True)
        self._data_tasks.clear()

    # ── Internals ───────────────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._worker_secret}"}
        if self._cf_client_id and self._cf_client_secret:
            headers["CF-Access-Client-Id"] = self._cf_client_id
            headers["CF-Access-Client-Secret"] = self._cf_client_secret
        return headers

    def _to_ws_scheme(self, url: str) -> str:
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    async def _run(self) -> None:
        """Control-channel reconnect loop. Runs until stop() is called."""
        backoff = _Backoff()
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff.reset()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                wait = backoff.next()
                logger.warning(
                    "hub_control_disconnected",
                    error=str(e),
                    retry_in=wait,
                    worker_id=self._worker_id,
                )
                # Sleep with early exit on stop().
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    return
                except TimeoutError:
                    continue

    async def _run_once(self) -> None:
        control_url = f"{self._to_ws_scheme(self._hub_url)}/worker-register"
        logger.info(
            "hub_control_dialing", url=control_url, worker_id=self._worker_id
        )
        async with websockets.connect(
            control_url,
            additional_headers=self._auth_headers(),
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
            # Default is 1 MiB (websockets library), which silently drops
            # any larger frame with close code 1009. Hub sends upload
            # commands containing base64-encoded files over the data
            # channel, so we need to match (or exceed) uvicorn's
            # ws_max_size on the hub. 32 MiB leaves headroom over the
            # 12 MiB raw upload limit + base64 + JSON envelope.
            max_size=32 * 1024 * 1024,
        ) as ws:
            await ws.send(json.dumps({
                "type": "register",
                "payload": {
                    "id": self._worker_id,
                    "type": self._worker_type,
                    "label": self._worker_label,
                    # Models left empty - hub queries list_models on the data
                    # channel once it opens, which already runs through the
                    # SDK's live model list (see ws/handler.py).
                    "models": [],
                },
            }))
            try:
                raw_ack = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except TimeoutError as exc:
                raise RuntimeError("hub did not ack register handshake in time") from exc
            ack = json.loads(raw_ack) if isinstance(raw_ack, str) else {}
            if ack.get("type") != "registered":
                raise RuntimeError(f"hub rejected register: {ack}")
            logger.info("hub_control_registered", worker_id=self._worker_id)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type")
                if msg_type == "open_data_channel":
                    session_id = (msg.get("payload") or {}).get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        logger.warning("hub_open_data_missing_session_id")
                        continue
                    task = asyncio.create_task(self._handle_data_session(session_id))
                    self._data_tasks.add(task)
                    task.add_done_callback(self._data_tasks.discard)
                elif msg_type == "pong":
                    pass
                else:
                    logger.debug(
                        "hub_control_unknown_msg",
                        msg_type=msg_type,
                        worker_id=self._worker_id,
                    )

    async def _handle_data_session(self, session_id: str) -> None:
        """Dial one data WS and run handle_websocket against it."""
        scheme_url = self._to_ws_scheme(self._hub_url)
        data_url = (
            f"{scheme_url}/worker-data"
            f"?worker_id={self._worker_id}&session_id={session_id}"
        )
        try:
            async with websockets.connect(
                data_url,
                additional_headers=self._auth_headers(),
                ping_interval=20,
                ping_timeout=60,
                close_timeout=5,
                # See max_size note in _run_once. Data channel carries
                # the actual upload payloads so this is the critical one.
                max_size=32 * 1024 * 1024,
            ) as ws:
                adapter = _ClientWSAdapter(ws)
                # Reuse session_id as the connection_id for log correlation;
                # device_label is a friendly tag the original handler uses
                # for lock takeover prompts - in reverse mode the device
                # label is supplied by the *frontend* on its own WS so this
                # is just a placeholder.
                logger.info(
                    "hub_data_session_open",
                    worker_id=self._worker_id,
                    session_id=session_id,
                )
                await handle_websocket(adapter, session_id, device_label="reverse-mode")
        except websockets.exceptions.ConnectionClosed:
            logger.info(
                "hub_data_session_closed",
                worker_id=self._worker_id,
                session_id=session_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "hub_data_session_error",
                worker_id=self._worker_id,
                session_id=session_id,
                error=str(e),
                exc_info=True,
            )
