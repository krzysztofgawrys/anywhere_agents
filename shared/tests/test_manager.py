"""Tests for worker_shared.sdk.manager - SessionManager lifecycle.

These tests verify the critical behavioral change: switching sessions now
parks the old session (agent keeps running) instead of stopping it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker_shared.locks.manager import LockManager
from worker_shared.sdk.manager import SessionManager
from worker_shared.sdk.registry import SessionRegistry


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_session(session_id: str = "s1", cwd: str = "/tmp/project") -> MagicMock:
    s = MagicMock()
    s.session_id = session_id
    s.cwd = cwd
    s.permissions = MagicMock()
    s.permissions.is_auto_approve = False
    s.permissions.cancel_permissions = MagicMock()
    s.start = AsyncMock()
    s.stop = AsyncMock()
    s.interrupt = AsyncMock()
    s.send_prompt = AsyncMock()
    s.set_auto_approve = MagicMock()
    s.set_model = AsyncMock()
    s.rebind = MagicMock()
    s.notify_reconnected = AsyncMock()
    return s


def _make_manager(
    *,
    sessions: list[MagicMock] | None = None,
    registry: SessionRegistry | None = None,
    lock_manager: LockManager | None = None,
) -> tuple[SessionManager, AsyncMock, list[MagicMock]]:
    """Build a SessionManager with injectable mocks."""
    sent: list[dict] = []

    async def fake_send(msg: dict) -> None:
        sent.append(msg)

    session_list = list(sessions or [])
    call_idx = {"i": 0}

    def factory(**kwargs: Any) -> MagicMock:
        if call_idx["i"] < len(session_list):
            s = session_list[call_idx["i"]]
            call_idx["i"] += 1
            # Ensure session_id matches what factory would set
            return s
        s = _make_session(session_id=f"auto-{call_idx['i']}")
        call_idx["i"] += 1
        return s

    mgr = SessionManager(
        send=fake_send,
        session_factory=factory,
        connection_id="conn-1",
        device_label="test",
        lock_manager=lock_manager or LockManager(),
    )
    return mgr, fake_send, session_list


# ── new_session ────────────────────────────────────────────────────────


async def test_new_session_creates_and_starts(tmp_path: Any) -> None:
    s = _make_session("new-1")
    mgr, _, _ = _make_manager(sessions=[s])
    result = await mgr.new_session(str(tmp_path))
    assert result is s
    s.start.assert_awaited_once()
    assert mgr.current is s


async def test_new_session_bad_cwd() -> None:
    mgr, send, _ = _make_manager()
    result = await mgr.new_session("/nonexistent/path/that/does/not/exist")
    assert result is None


async def test_new_session_parks_old(tmp_path: Any) -> None:
    """Switching to a new session parks the old one (not stops)."""
    old = _make_session("old")
    new = _make_session("new")
    reg = SessionRegistry()
    mgr, _, _ = _make_manager(sessions=[old, new], registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        assert mgr.current is old

        await mgr.new_session(str(tmp_path))
        assert mgr.current is new

        # Old session was PARKED, not stopped
        old.stop.assert_not_awaited()
        assert reg.has("old")


# ── resume_session: fast path (parked in registry) ─────────────────────


async def test_resume_fast_path(tmp_path: Any) -> None:
    """Resume picks up a parked session from the registry."""
    parked_session = _make_session("s1")
    reg = SessionRegistry()
    reg.park(parked_session)

    mgr, _, _ = _make_manager(registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "s1")

    assert result is parked_session
    parked_session.rebind.assert_called()
    parked_session.notify_reconnected.assert_awaited_once()


async def test_resume_fast_path_parks_old(tmp_path: Any) -> None:
    """Resume parks the previously active session."""
    old = _make_session("old")
    parked = _make_session("target")
    reg = SessionRegistry()

    mgr, _, _ = _make_manager(sessions=[old], registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        # Create initial session
        await mgr.new_session(str(tmp_path))
        assert mgr.current is old

        # Park target so resume can find it
        reg.park(parked)

        # Resume target - should park old
        await mgr.resume_session(str(tmp_path), "target")
        assert mgr.current is parked
        old.stop.assert_not_awaited()
        assert reg.has("old")


# ── resume_session: slow path (from disk) ──────────────────────────────


async def test_resume_slow_path(tmp_path: Any) -> None:
    """When session is not in registry, factory creates a new one."""
    s = _make_session("disk-resume")
    reg = SessionRegistry()
    mgr, _, _ = _make_manager(sessions=[s], registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "disk-resume")

    assert result is s
    s.start.assert_awaited_once()


# ── resume_session: lock conflict ──────────────────────────────────────


async def test_resume_lock_conflict(tmp_path: Any) -> None:
    """Resume fails when another connection holds the lock."""
    lm = LockManager()
    reg = SessionRegistry()

    # Another connection holds the lock
    await lm.try_acquire(
        session_id="s1", connection_id="other-conn",
        device_label="other", notify=AsyncMock(),
    )

    mgr, _, _ = _make_manager(lock_manager=lm, registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "s1")

    assert result is None


# ── stop (WS disconnect) parks, does not stop ──────────────────────────


async def test_stop_parks_session(tmp_path: Any) -> None:
    """WS disconnect parks the session for later reclaim."""
    s = _make_session("s1")
    reg = SessionRegistry()
    mgr, _, _ = _make_manager(sessions=[s], registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        await mgr.stop()

    assert mgr.current is None
    assert reg.has("s1")
    s.stop.assert_not_awaited()


# ── Lock revocation stops (not parks) ──────────────────────────────────


async def test_lock_revocation_stops_session(tmp_path: Any) -> None:
    """Force takeover by another client fully stops the session."""
    s = _make_session("s1")
    lm = LockManager()
    reg = SessionRegistry()
    mgr, _, _ = _make_manager(sessions=[s], lock_manager=lm, registry=reg)

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        assert mgr.current is s

        # Another connection force-takes the lock
        await lm.try_acquire(
            session_id="s1", connection_id="attacker",
            device_label="phone", notify=AsyncMock(), force=True,
        )

        # The revocation callback should have stopped (not parked) the session
        s.stop.assert_awaited_once()
        assert mgr.current is None
        assert not reg.has("s1")


# ── Pass-through methods ───────────────────────────────────────────────


async def test_interrupt_forwards(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, _, _ = _make_manager(sessions=[s])
    await mgr.new_session(str(tmp_path))
    await mgr.interrupt()
    s.interrupt.assert_awaited_once()


async def test_send_prompt_no_session() -> None:
    mgr, _, _ = _make_manager()
    result = await mgr.send_prompt("hello")
    assert result is False


async def test_set_model_forwards(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, _, _ = _make_manager(sessions=[s])
    await mgr.new_session(str(tmp_path))
    await mgr.set_model("gpt-4")
    s.set_model.assert_awaited_once_with("gpt-4")
