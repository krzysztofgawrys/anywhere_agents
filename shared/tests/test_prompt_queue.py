"""Tests for the prompt queue + delivery acks.

These cover the ways a user prompt used to disappear:
  - sent while the agent was mid-turn (compaction / consolidation) -> was
    rejected with `busy` and dropped; must now queue and run afterwards,
  - retried after a reconnect -> must be acked, never run twice,
  - backlog full / no session -> must be refused loudly (with the client's
    id) rather than accepted and forgotten.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker_shared.sdk import prompt_queue
from worker_shared.sdk.manager import SessionManager
from worker_shared.sdk.registry import SessionRegistry


@pytest.fixture(autouse=True)
def _clean_prompt_queue() -> Any:
    prompt_queue._queues.clear()
    prompt_queue._seen.clear()
    yield
    prompt_queue._queues.clear()
    prompt_queue._seen.clear()


class FakeSession:
    """Minimal session with a controllable busy flag."""

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.cwd = "/tmp/project"
        self.permissions = MagicMock()
        self.permissions.is_auto_approve = False
        self.prompts: list[str] = []
        self._busy = False
        self._idle = asyncio.Event()
        self._idle.set()
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.set_auto_approve = MagicMock()
        self.set_model = AsyncMock()
        self.set_effort = AsyncMock()
        self.rebind = MagicMock()
        self.notify_reconnected = AsyncMock()

    @property
    def is_busy(self) -> bool:
        return self._busy

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def begin_turn(self) -> None:
        self._busy = True
        self._idle.clear()

    def end_turn(self) -> None:
        self._busy = False
        self._idle.set()

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        # Real sessions go busy for the duration of the turn; mimic that so
        # the queue's ordering guarantees are actually exercised.
        self.prompts.append(text)
        self.begin_turn()


def _make_manager(session: FakeSession) -> tuple[SessionManager, list[dict]]:
    sent: list[dict] = []

    async def fake_send(msg: dict) -> None:
        sent.append(msg)

    mgr = SessionManager(
        fake_send,
        session_factory=lambda **_: session,
        connection_id="c1",
    )
    mgr._current = session  # type: ignore[assignment]
    return mgr, sent


def _acks(sent: list[dict]) -> list[dict]:
    return [m["payload"] for m in sent if m["type"] == "prompt_ack"]


def _errors(sent: list[dict]) -> list[dict]:
    return [m["payload"] for m in sent if m["type"] == "error"]


# ── Happy path ──────────────────────────────────────────────────────────


async def test_idle_prompt_dispatches_and_acks() -> None:
    session = FakeSession()
    mgr, sent = _make_manager(session)

    assert await mgr.send_prompt("hello", client_msg_id="m1") is True

    assert session.prompts == ["hello"]
    assert _acks(sent) == [{"client_msg_id": "m1", "status": "accepted"}]


async def test_prompt_without_client_id_is_not_acked() -> None:
    """Older frontends have no outbox, so there is nothing to confirm to."""
    session = FakeSession()
    mgr, sent = _make_manager(session)

    await mgr.send_prompt("hello")

    assert session.prompts == ["hello"]
    assert _acks(sent) == []


# ── The compaction case: prompt arrives mid-turn ────────────────────────


async def test_busy_prompt_is_queued_then_run_when_idle() -> None:
    session = FakeSession()
    mgr, sent = _make_manager(session)
    session.begin_turn()  # agent is mid-turn (e.g. auto-compaction)

    assert await mgr.send_prompt("while busy", client_msg_id="m1") is True

    # Queued, acked as such, and NOT rejected with `busy`.
    assert session.prompts == []
    assert _acks(sent) == [{"client_msg_id": "m1", "status": "queued"}]
    assert _errors(sent) == []
    assert prompt_queue.depth("s1") == 1

    session.end_turn()
    await asyncio.sleep(0)  # let the pump run
    await asyncio.sleep(0)

    assert session.prompts == ["while busy"]
    assert prompt_queue.depth("s1") == 0


async def test_queue_preserves_order() -> None:
    session = FakeSession()
    mgr, _ = _make_manager(session)
    session.begin_turn()

    for i in range(3):
        await mgr.send_prompt(f"p{i}", client_msg_id=f"m{i}")
    assert prompt_queue.depth("s1") == 3

    # Each dispatch makes the session busy again, so drain turn by turn.
    for i in range(3):
        session.end_turn()
        for _ in range(4):
            await asyncio.sleep(0)
        assert session.prompts[-1] == f"p{i}"

    assert session.prompts == ["p0", "p1", "p2"]


# ── Retries ─────────────────────────────────────────────────────────────


async def test_duplicate_client_msg_id_is_reacked_not_rerun() -> None:
    """A retry whose first ack died with the socket must not run twice."""
    session = FakeSession()
    mgr, sent = _make_manager(session)

    await mgr.send_prompt("hello", client_msg_id="m1")
    session.end_turn()
    assert await mgr.send_prompt("hello", client_msg_id="m1") is True

    assert session.prompts == ["hello"]
    assert _acks(sent) == [
        {"client_msg_id": "m1", "status": "accepted"},
        {"client_msg_id": "m1", "status": "duplicate"},
    ]


async def test_distinct_ids_both_run() -> None:
    session = FakeSession()
    mgr, _ = _make_manager(session)

    await mgr.send_prompt("first", client_msg_id="m1")
    session.end_turn()
    await mgr.send_prompt("second", client_msg_id="m2")

    assert session.prompts == ["first", "second"]


# ── Refusals must be reported, with the id ──────────────────────────────


async def test_no_session_error_carries_client_msg_id() -> None:
    session = FakeSession()
    mgr, sent = _make_manager(session)
    mgr._current = None  # type: ignore[assignment]

    assert await mgr.send_prompt("hello", client_msg_id="m1") is False

    assert _errors(sent) == [
        {
            "code": "no_session",
            "message": "Start or resume a session before sending prompts",
            "client_msg_id": "m1",
        }
    ]
    # The id must NOT be burned: the client's retry has to be accepted.
    assert prompt_queue.mark_seen("m1") is True


async def test_full_queue_is_refused_and_id_released() -> None:
    session = FakeSession()
    mgr, sent = _make_manager(session)
    session.begin_turn()

    with patch.object(prompt_queue, "MAX_QUEUE_DEPTH", 1):
        assert await mgr.send_prompt("first", client_msg_id="m1") is True
        assert await mgr.send_prompt("second", client_msg_id="m2") is False

    errors = _errors(sent)
    assert len(errors) == 1
    assert errors[0]["code"] == "queue_full"
    assert errors[0]["client_msg_id"] == "m2"
    # Refused prompts keep their retry rights; accepted ones don't.
    assert prompt_queue.mark_seen("m2") is True
    assert prompt_queue.mark_seen("m1") is False


# ── Queue survives the connection, dies with the session ────────────────


async def test_queue_survives_a_new_manager() -> None:
    """A browser reconnect builds a fresh manager; the backlog must live on."""
    session = FakeSession()
    mgr1, _ = _make_manager(session)
    session.begin_turn()
    await mgr1.send_prompt("queued before reconnect", client_msg_id="m1")

    # Simulate reconnect: old manager parks the session, new one reattaches.
    reg = SessionRegistry()
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr1.stop()
        mgr2, sent2 = _make_manager(session)
        mgr2._current = session  # type: ignore[assignment]
        await mgr2.send_prompt("sent after reconnect", client_msg_id="m2")

    assert prompt_queue.depth("s1") == 2
    session.end_turn()
    for _ in range(6):
        await asyncio.sleep(0)
    assert session.prompts == ["queued before reconnect"]
    assert _acks(sent2) == [{"client_msg_id": "m2", "status": "queued"}]


async def test_discard_drops_backlog() -> None:
    session = FakeSession()
    mgr, _ = _make_manager(session)
    session.begin_turn()
    await mgr.send_prompt("doomed", client_msg_id="m1")
    assert prompt_queue.depth("s1") == 1

    prompt_queue.discard("s1")

    assert prompt_queue.depth("s1") == 0
    session.end_turn()
    for _ in range(4):
        await asyncio.sleep(0)
    assert session.prompts == []
