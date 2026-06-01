"""Tests for worker_shared.sdk.manager - SessionManager lifecycle.

Black-box approach: test observable outcomes (what messages get sent, what
locks exist, what the registry contains) rather than asserting on internal
mock call patterns. Each test sets up state, performs an action, then
checks ALL the things that should be true - not just the one thing we
expect to change.
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
    lock_manager: LockManager | None = None,
) -> tuple[SessionManager, list[dict], LockManager, SessionRegistry]:
    """Build a SessionManager with injectable mocks.

    Returns (manager, sent_messages, lock_manager, registry).
    The registry is always fresh and patched into the module.
    """
    sent: list[dict] = []

    async def fake_send(msg: dict) -> None:
        sent.append(msg)

    session_list = list(sessions or [])
    call_idx = {"i": 0}

    def factory(**kwargs: Any) -> MagicMock:
        if call_idx["i"] < len(session_list):
            s = session_list[call_idx["i"]]
            call_idx["i"] += 1
            return s
        s = _make_session(session_id=f"auto-{call_idx['i']}")
        call_idx["i"] += 1
        return s

    lm = lock_manager or LockManager()
    mgr = SessionManager(
        send=fake_send,
        session_factory=factory,
        connection_id="conn-1",
        device_label="test",
        lock_manager=lm,
    )
    reg = SessionRegistry()
    return mgr, sent, lm, reg


# ── new_session ────────────────────────────────────────────────────────


async def test_new_session_creates_starts_and_locks(tmp_path: Any) -> None:
    s = _make_session("new-1")
    mgr, sent, lm, reg = _make_manager(sessions=[s])
    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.new_session(str(tmp_path))

    assert result is s
    assert mgr.current is s
    s.start.assert_awaited_once()
    # Lock acquired for this session
    assert lm.info("new-1") is not None
    assert lm.info("new-1").connection_id == "conn-1"


async def test_new_session_bad_cwd_sends_error() -> None:
    mgr, sent, lm, reg = _make_manager()
    result = await mgr.new_session("/nonexistent/path/xyz")
    assert result is None
    assert mgr.current is None
    # Should have sent an error message
    assert any(m["type"] == "error" and m["payload"]["code"] == "cwd_not_found" for m in sent)
    # No locks acquired
    assert lm.active_locks == 0


async def test_new_session_parks_old_and_releases_lock(tmp_path: Any) -> None:
    """Switching to a new session parks the old one AND releases its lock."""
    old = _make_session("old")
    new = _make_session("new")
    mgr, sent, lm, reg = _make_manager(sessions=[old, new])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        assert lm.info("old") is not None  # old lock held

        await mgr.new_session(str(tmp_path))

    # New is current, old is parked (not stopped)
    assert mgr.current is new
    old.stop.assert_not_awaited()
    assert reg.has("old")
    # Old lock released, new lock held
    assert lm.info("old") is None
    assert lm.info("new") is not None


async def test_new_session_old_session_rebind_called(tmp_path: Any) -> None:
    """Parked session gets rebind(noop, parked=True) so it doesn't write to dead WS."""
    old = _make_session("old")
    new = _make_session("new")
    mgr, _, _, reg = _make_manager(sessions=[old, new])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        await mgr.new_session(str(tmp_path))

    # registry.park() calls rebind(noop, parked=True)
    old.rebind.assert_called_once()
    _, kwargs = old.rebind.call_args
    assert kwargs.get("parked") is True


# ── resume_session: fast path (parked in registry) ─────────────────────


async def test_resume_fast_path_reclaims_and_notifies(tmp_path: Any) -> None:
    parked_session = _make_session("s1")
    mgr, sent, lm, reg = _make_manager()
    reg.park(parked_session)

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "s1")

    assert result is parked_session
    assert mgr.current is parked_session
    # Session removed from registry (reclaimed)
    assert not reg.has("s1")
    # rebind called with real send (not noop), parked=False implied
    parked_session.notify_reconnected.assert_awaited_once()
    # Lock acquired
    assert lm.info("s1") is not None
    # start() NOT called (fast path reuses existing session)
    parked_session.start.assert_not_awaited()


async def test_resume_fast_path_parks_old_releases_old_lock(tmp_path: Any) -> None:
    old = _make_session("old")
    target = _make_session("target")
    mgr, sent, lm, reg = _make_manager(sessions=[old])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        assert lm.info("old") is not None

        reg.park(target)
        await mgr.resume_session(str(tmp_path), "target")

    assert mgr.current is target
    old.stop.assert_not_awaited()
    assert reg.has("old")
    # Old lock released, target lock held
    assert lm.info("old") is None
    assert lm.info("target") is not None


async def test_resume_fast_path_model_override(tmp_path: Any) -> None:
    """Model passed in resume overrides the parked session's model."""
    s = _make_session("s1")
    mgr, _, _, reg = _make_manager()
    reg.park(s)

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.resume_session(str(tmp_path), "s1", model="claude-sonnet")

    s.set_model.assert_awaited_once_with("claude-sonnet")


async def test_resume_fast_path_no_model_does_not_override(tmp_path: Any) -> None:
    """Resume without model= doesn't clobber parked session's model."""
    s = _make_session("s1")
    mgr, _, _, reg = _make_manager()
    reg.park(s)

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.resume_session(str(tmp_path), "s1")

    s.set_model.assert_not_awaited()


# ── resume_session: slow path (from disk) ──────────────────────────────


async def test_resume_slow_path_creates_and_starts(tmp_path: Any) -> None:
    s = _make_session("disk-resume")
    mgr, _, lm, reg = _make_manager(sessions=[s])

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "disk-resume")

    assert result is s
    assert mgr.current is s
    s.start.assert_awaited_once()
    assert lm.info("disk-resume") is not None


async def test_resume_slow_path_parks_old(tmp_path: Any) -> None:
    old = _make_session("old")
    new = _make_session("new")
    mgr, _, lm, reg = _make_manager(sessions=[old, new])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        await mgr.resume_session(str(tmp_path), "new")

    assert mgr.current is new
    old.stop.assert_not_awaited()
    assert reg.has("old")
    assert lm.info("old") is None
    assert lm.info("new") is not None


# ── resume_session: lock conflict ──────────────────────────────────────


async def test_resume_lock_conflict_sends_session_locked(tmp_path: Any) -> None:
    lm = LockManager()
    await lm.try_acquire(
        session_id="s1", connection_id="other-conn",
        device_label="other-phone", notify=AsyncMock(),
    )

    mgr, sent, _, reg = _make_manager(lock_manager=lm)

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "s1")

    assert result is None
    assert mgr.current is None
    # Should send session_locked with the holder's info
    locked_msgs = [m for m in sent if m["type"] == "session_locked"]
    assert len(locked_msgs) == 1
    assert locked_msgs[0]["payload"]["locked_by"] == "other-phone"


async def test_resume_lock_conflict_parked_session_returned_to_registry(tmp_path: Any) -> None:
    """If a parked session exists but another client holds its lock, re-park it."""
    s = _make_session("s1")
    lm = LockManager()
    await lm.try_acquire(
        session_id="s1", connection_id="other-conn",
        device_label="other", notify=AsyncMock(),
    )
    mgr, _, _, reg = _make_manager(lock_manager=lm)
    reg.park(s)

    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session(str(tmp_path), "s1")

    assert result is None
    # Session put back in registry, not lost
    assert reg.has("s1")


async def test_resume_bad_cwd(tmp_path: Any) -> None:
    mgr, sent, _, reg = _make_manager()
    with patch("worker_shared.sdk.manager.registry", reg):
        result = await mgr.resume_session("/nonexistent/xyz", "s1")
    assert result is None
    assert any(m["type"] == "error" and m["payload"]["code"] == "cwd_not_found" for m in sent)


# ── stop (WS disconnect) ──────────────────────────────────────────────


async def test_stop_parks_and_releases_all_locks(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, _, lm, reg = _make_manager(sessions=[s])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        assert lm.active_locks == 1

        await mgr.stop()

    assert mgr.current is None
    assert reg.has("s1")
    s.stop.assert_not_awaited()
    # All locks released
    assert lm.active_locks == 0


async def test_stop_with_no_session_is_noop() -> None:
    mgr, _, lm, reg = _make_manager()
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.stop()  # should not raise
    assert reg.parked_count == 0


# ── Lock revocation ──────────────────────────────────────────────────


async def test_lock_revocation_stops_and_sends_lock_revoked(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, sent, lm, reg = _make_manager(sessions=[s])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        sent.clear()

        await lm.try_acquire(
            session_id="s1", connection_id="attacker",
            device_label="phone", notify=AsyncMock(), force=True,
        )

    # Session fully stopped (not parked)
    s.stop.assert_awaited_once()
    assert mgr.current is None
    assert not reg.has("s1")
    # lock_revoked message sent to client
    revoked = [m for m in sent if m["type"] == "lock_revoked"]
    assert len(revoked) == 1


async def test_lock_revocation_wrong_session_ignored(tmp_path: Any) -> None:
    """Revocation for a different session_id doesn't touch current."""
    s = _make_session("s1")
    lm = LockManager()
    mgr, _, _, reg = _make_manager(sessions=[s], lock_manager=lm)

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))

        # Acquire + revoke lock for a DIFFERENT session
        await lm.try_acquire(
            session_id="other", connection_id="conn-1",
            device_label="test", notify=AsyncMock(),
        )
        await lm.try_acquire(
            session_id="other", connection_id="attacker",
            device_label="phone", notify=AsyncMock(), force=True,
        )

    # Current session untouched
    assert mgr.current is s
    s.stop.assert_not_awaited()


# ── Pass-through methods ───────────────────────────────────────────────


async def test_interrupt_forwards(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, _, _, reg = _make_manager(sessions=[s])
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
    await mgr.interrupt()
    s.interrupt.assert_awaited_once()


async def test_interrupt_no_session_is_noop() -> None:
    mgr, _, _, _ = _make_manager()
    await mgr.interrupt()  # should not raise


async def test_send_prompt_no_session_sends_error() -> None:
    mgr, sent, _, _ = _make_manager()
    result = await mgr.send_prompt("hello")
    assert result is False
    assert any(m["type"] == "error" and m["payload"]["code"] == "no_session" for m in sent)


async def test_send_prompt_forwards(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, _, _, reg = _make_manager(sessions=[s])
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
    result = await mgr.send_prompt("hello", auto_approve_once=True, stream=True)
    assert result is True
    s.send_prompt.assert_awaited_once_with(
        "hello", auto_approve_once=True, images=None, stream=True,
    )


async def test_set_model_forwards(tmp_path: Any) -> None:
    s = _make_session("s1")
    mgr, _, _, reg = _make_manager(sessions=[s])
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
    await mgr.set_model("gpt-4")
    s.set_model.assert_awaited_once_with("gpt-4")


async def test_set_model_no_session_is_noop() -> None:
    mgr, _, _, _ = _make_manager()
    await mgr.set_model("gpt-4")  # should not raise


async def test_resolve_permission_forwards(tmp_path: Any) -> None:
    s = _make_session("s1")
    s.permissions.resolve.return_value = True
    mgr, _, _, reg = _make_manager(sessions=[s])
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
    assert mgr.resolve_permission("tool-1", allow=True, reason="ok") is True
    s.permissions.resolve.assert_called_once_with("tool-1", allow=True, reason="ok")


async def test_resolve_permission_no_session() -> None:
    mgr, _, _, _ = _make_manager()
    assert mgr.resolve_permission("tool-1", allow=True) is False


# ── Edge cases ─────────────────────────────────────────────────────────


async def test_park_then_resume_same_session(tmp_path: Any) -> None:
    """Create session, switch away (parks it), switch back (reclaims it)."""
    s1 = _make_session("s1")
    s2 = _make_session("s2")
    mgr, _, lm, reg = _make_manager(sessions=[s1, s2])

    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))  # s1 active
        await mgr.new_session(str(tmp_path))  # s2 active, s1 parked

        assert reg.has("s1")
        result = await mgr.resume_session(str(tmp_path), "s1")  # s1 back, s2 parked

    assert result is s1
    assert mgr.current is s1
    assert not reg.has("s1")
    assert reg.has("s2")
    s1.stop.assert_not_awaited()
    s2.stop.assert_not_awaited()


async def test_rapid_session_switches(tmp_path: Any) -> None:
    """Switch through 3 sessions - all parked, none stopped."""
    sessions = [_make_session(f"s{i}") for i in range(3)]
    mgr, _, _, reg = _make_manager(sessions=sessions)

    with patch("worker_shared.sdk.manager.registry", reg):
        for s in sessions:
            await mgr.new_session(str(tmp_path))

    assert mgr.current is sessions[2]
    assert reg.has("s0")
    assert reg.has("s1")
    assert not reg.has("s2")
    for s in sessions:
        s.stop.assert_not_awaited()
