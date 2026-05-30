"""Inbound worker registry - control + data channel pairing.

Reverse mode flow:

  1. Worker dials hub at /worker-register (long-lived control WS), passes
     auth + identifies itself with {type: "register", id, type, label,
     models}. Hub stores the control connection here keyed by worker_id.

  2. When a browser connects and the hub WS handler decides it needs to
     talk to that worker, it calls open_session(worker_id, session_id).
     The registry sends {type: "open_data_channel", session_id} over the
     control WS and returns an asyncio.Future awaiting the matching data
     WS.

  3. The worker reacts by dialing /worker-data?worker_id=...&session_id=...
     (a fresh, short-lived WS, one per browser session). The hub endpoint
     calls register_data(session_id, ws) which resolves the waiter created
     in step 2.

  4. open_session() returns the accepted FastAPI WS. The caller wraps it in
     a WorkerConnection via .adopt(ws) and uses it like any other worker
     connection. When the browser disconnects, WorkerConnection.close()
     closes the data WS; the worker handler sees the disconnect and tears
     down its session normally. Control WS lives on for the next browser.

Single global instance: `inbound_registry`. Hub is one process so no need
for cross-process coordination.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import WebSocket

logger = structlog.get_logger()

# How long open_session() will wait for the worker to dial back with the
# matching data WS before giving up. Worker is right there at the other end
# of the control channel, so this only needs to cover network RTT + the
# small amount of time it takes the worker's dialer to spawn a task and
# open a fresh WS. Generous default leaves slack for cold containers.
DEFAULT_OPEN_TIMEOUT_S = 15.0


@dataclass
class _ControlEntry:
    worker_id: str
    ws: WebSocket
    worker_type: str = "claude"
    label: str = ""
    models: list[dict[str, str]] = field(default_factory=list)
    # Locks the writer side so multiple open_session() callers don't
    # interleave JSON frames on the same control WS.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InboundRegistry:
    """Pairs control-channel commands with the data WS the worker dials."""

    def __init__(self) -> None:
        self._controls: dict[str, _ControlEntry] = {}
        # session_id -> Future that resolves with the data WS
        self._waiters: dict[str, asyncio.Future[WebSocket]] = {}

    # ── Control side ────────────────────────────────────────────────────

    def register_control(
        self,
        worker_id: str,
        ws: WebSocket,
        worker_type: str = "claude",
        label: str = "",
        models: Iterable[dict[str, str]] | None = None,
    ) -> _ControlEntry:
        """Store an active control WS for this worker.

        If a control was already registered for the same worker_id (worker
        reconnected after a network blip), drop the stale one and replace.
        Returns the entry so callers can use the write lock if needed.
        """
        existing = self._controls.get(worker_id)
        if existing is not None:
            logger.warning("worker_control_replaced", worker_id=worker_id)
        entry = _ControlEntry(
            worker_id=worker_id,
            ws=ws,
            worker_type=worker_type,
            label=label,
            models=list(models or []),
        )
        self._controls[worker_id] = entry
        logger.info("worker_control_registered", worker_id=worker_id, type=worker_type)
        return entry

    def drop_control(self, worker_id: str, ws: WebSocket | None = None) -> None:
        """Remove the control entry for this worker.

        If `ws` is given, only drop the entry if it still points at that
        exact WS - this avoids the race where worker A drops, worker A
        reconnects (replaces), and then the original drop callback fires
        and would otherwise remove the fresh control.
        """
        entry = self._controls.get(worker_id)
        if entry is None:
            return
        if ws is not None and entry.ws is not ws:
            return
        self._controls.pop(worker_id, None)
        logger.info("worker_control_dropped", worker_id=worker_id)

    def has_control(self, worker_id: str) -> bool:
        return worker_id in self._controls

    def get_control_meta(self, worker_id: str) -> _ControlEntry | None:
        return self._controls.get(worker_id)

    # ── Session pairing ─────────────────────────────────────────────────

    async def open_session(
        self,
        worker_id: str,
        timeout: float = DEFAULT_OPEN_TIMEOUT_S,
    ) -> WebSocket:
        """Ask the worker to open a data channel; return it when paired.

        Raises:
            LookupError: if no control connection for worker_id is active.
            TimeoutError: if the worker doesn't dial back in time.
        """
        entry = self._controls.get(worker_id)
        if entry is None:
            raise LookupError(f"no control channel for worker {worker_id}")

        session_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        waiter: asyncio.Future[WebSocket] = loop.create_future()
        self._waiters[session_id] = waiter

        cmd = {
            "type": "open_data_channel",
            "payload": {"session_id": session_id},
        }
        try:
            async with entry.write_lock:
                await entry.ws.send_text(json.dumps(cmd))
        except Exception as e:
            self._waiters.pop(session_id, None)
            logger.warning("worker_open_data_send_failed", worker_id=worker_id, error=str(e))
            # Drop the broken control so the next open_session() raises
            # LookupError instead of trying to write into a dead WS again.
            self.drop_control(worker_id, entry.ws)
            raise LookupError(f"control channel for worker {worker_id} broken") from e

        try:
            return await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError:
            # Clean up the orphan waiter so the worker, if it eventually
            # dials with this stale session_id, will be cleanly rejected
            # by register_data() returning False.
            self._waiters.pop(session_id, None)
            logger.warning(
                "worker_open_data_timeout", worker_id=worker_id, session_id=session_id
            )
            raise

    def register_data(self, session_id: str, ws: WebSocket) -> bool:
        """Resolve the open_session() waiter for this session_id.

        Returns True if a waiter was found and resolved; False if no waiter
        existed (stale session_id, e.g. open_session timed out before the
        worker got there). Caller should close the WS on False.
        """
        future = self._waiters.pop(session_id, None)
        if future is None or future.done():
            return False
        future.set_result(ws)
        return True

    # ── Introspection (mostly for tests / logging) ──────────────────────

    def connected_worker_ids(self) -> list[str]:
        return list(self._controls.keys())


# Process-wide singleton. Hub is one uvicorn worker so no cross-process
# coordination needed; that's a deliberate constraint (matches how
# project_index, push_manager, etc. live as module-level singletons).
inbound_registry = InboundRegistry()
