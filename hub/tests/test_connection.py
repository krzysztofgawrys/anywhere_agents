"""WorkerConnection.request() correlation tests.

Regression coverage for the per-response_type lock: two concurrent
request() calls expecting the same response_type on one connection must
not overwrite each other's pending future (which previously orphaned the
loser into a timeout and leaked the extra worker response to on_message).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from src.workers.connection import WorkerConnection


class FakeTransport:
    """In-memory transport: queue for inbound, list capturing outbound."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self._alive = True

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv_text(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise websockets.exceptions.ConnectionClosed(None, None)
        return item

    async def close(self) -> None:
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive


def _make_conn(received: list[dict[str, Any]]) -> tuple[WorkerConnection, FakeTransport]:
    async def on_message(msg: dict[str, Any]) -> None:
        received.append(msg)

    conn = WorkerConnection("ws://x", "secret", "dev", on_message)
    transport = FakeTransport()
    conn._transport = transport  # type: ignore[assignment]
    conn._reader_task = asyncio.create_task(conn._read_loop())
    return conn, transport


async def _wait_until(pred: Any, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def test_concurrent_same_type_requests_dont_collide() -> None:
    received: list[dict[str, Any]] = []
    conn, transport = _make_conn(received)
    try:
        a = asyncio.create_task(
            conn.request({"type": "list_projects"}, "projects", timeout=2.0)
        )
        b = asyncio.create_task(
            conn.request({"type": "list_projects"}, "projects", timeout=2.0)
        )

        # Only A should have sent; B is blocked on the per-type lock.
        await _wait_until(lambda: len(transport.sent) == 1)
        await asyncio.sleep(0.05)
        assert len(transport.sent) == 1

        await transport.incoming.put(
            json.dumps({"type": "projects", "payload": {"who": "A"}})
        )
        # A resolves and releases the lock, so B now sends.
        await _wait_until(lambda: len(transport.sent) == 2)
        await transport.incoming.put(
            json.dumps({"type": "projects", "payload": {"who": "B"}})
        )

        ra, rb = await asyncio.gather(a, b)
        assert ra["payload"]["who"] == "A"
        assert rb["payload"]["who"] == "B"
        # Neither response leaked to on_message.
        assert received == []
    finally:
        await conn.close()


async def test_different_type_requests_run_concurrently() -> None:
    received: list[dict[str, Any]] = []
    conn, transport = _make_conn(received)
    try:
        a = asyncio.create_task(
            conn.request({"type": "list_projects"}, "projects", timeout=2.0)
        )
        b = asyncio.create_task(
            conn.request({"type": "list_models"}, "models", timeout=2.0)
        )

        # Different response_types -> different locks -> both send immediately.
        await _wait_until(lambda: len(transport.sent) == 2)

        await transport.incoming.put(
            json.dumps({"type": "models", "payload": {"x": 1}})
        )
        await transport.incoming.put(
            json.dumps({"type": "projects", "payload": {"y": 2}})
        )

        ra, rb = await asyncio.gather(a, b)
        assert ra["payload"]["y"] == 2
        assert rb["payload"]["x"] == 1
    finally:
        await conn.close()


async def test_request_includes_req_id_and_matches_echo() -> None:
    received: list[dict[str, Any]] = []
    conn, transport = _make_conn(received)
    try:
        task = asyncio.create_task(
            conn.request({"type": "list_projects"}, "projects", timeout=2.0)
        )
        await _wait_until(lambda: len(transport.sent) == 1)
        rid = transport.sent[0].get("_req_id")
        assert isinstance(rid, str) and rid
        await transport.incoming.put(
            json.dumps({"type": "projects", "_req_id": rid, "payload": {"ok": 1}})
        )
        resp = await task
        assert resp["payload"]["ok"] == 1
        assert received == []
    finally:
        await conn.close()


async def test_unsolicited_push_not_consumed_after_req_id() -> None:
    received: list[dict[str, Any]] = []
    conn, transport = _make_conn(received)
    try:
        # First request + echoed response flips the connection to req_id mode.
        t1 = asyncio.create_task(
            conn.request({"type": "list_projects"}, "projects", timeout=2.0)
        )
        await _wait_until(lambda: len(transport.sent) == 1)
        rid1 = transport.sent[0]["_req_id"]
        await transport.incoming.put(
            json.dumps({"type": "projects", "_req_id": rid1, "payload": {"n": 1}})
        )
        assert (await t1)["payload"]["n"] == 1

        # Second request in flight.
        t2 = asyncio.create_task(
            conn.request({"type": "list_projects"}, "projects", timeout=2.0)
        )
        await _wait_until(lambda: len(transport.sent) == 2)
        rid2 = transport.sent[1]["_req_id"]

        # An unsolicited push WITHOUT _req_id must NOT resolve t2; it must be
        # forwarded to on_message (this is the aliasing bug the fix closes).
        await transport.incoming.put(
            json.dumps({"type": "projects", "payload": {"unsolicited": True}})
        )
        await _wait_until(lambda: len(received) == 1)
        assert received[0]["payload"].get("unsolicited") is True
        assert not t2.done()

        # The real response with the echoed id resolves t2.
        await transport.incoming.put(
            json.dumps({"type": "projects", "_req_id": rid2, "payload": {"n": 2}})
        )
        assert (await t2)["payload"]["n"] == 2
    finally:
        await conn.close()
