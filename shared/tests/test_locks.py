"""Tests for worker_shared.locks.manager - per-session lock registry."""

import asyncio

import pytest

from worker_shared.locks.manager import LockManager


@pytest.fixture
def lm() -> LockManager:
    return LockManager()


async def _noop_notify(msg: dict) -> None:  # noqa: ARG001
    pass


# ── Acquire / release basics ───────────────────────────────────────────


async def test_acquire_fresh_lock(lm: LockManager) -> None:
    acquired, prior = await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    assert acquired is True
    assert prior is None
    assert lm.active_locks == 1


async def test_release_own_lock(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    released = await lm.release("s1", "c1")
    assert released is True
    assert lm.active_locks == 0


async def test_release_wrong_connection(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    released = await lm.release("s1", "other")
    assert released is False
    assert lm.active_locks == 1


async def test_release_nonexistent(lm: LockManager) -> None:
    released = await lm.release("nope", "c1")
    assert released is False


# ── Re-acquire by same connection ──────────────────────────────────────


async def test_reacquire_same_connection(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    acquired, _ = await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    assert acquired is True
    assert lm.active_locks == 1


# ── Conflict (different connection, same session) ──────────────────────


async def test_conflict_without_force(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    acquired, existing = await lm.try_acquire(
        session_id="s1", connection_id="c2", device_label="phone", notify=_noop_notify,
    )
    assert acquired is False
    assert existing is not None
    assert existing.connection_id == "c1"


async def test_force_takeover(lm: LockManager) -> None:
    revoked_msgs: list[dict] = []

    async def capture_notify(msg: dict) -> None:
        revoked_msgs.append(msg)

    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=capture_notify,
    )
    acquired, prior = await lm.try_acquire(
        session_id="s1", connection_id="c2", device_label="phone",
        notify=_noop_notify, force=True,
    )
    assert acquired is True
    assert prior is not None
    assert prior.connection_id == "c1"
    assert len(revoked_msgs) == 1
    assert revoked_msgs[0]["type"] == "lock_revoked"


async def test_same_device_label_silent_takeover(lm: LockManager) -> None:
    """Same device label = reconnect race; takeover without force."""
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    acquired, _ = await lm.try_acquire(
        session_id="s1", connection_id="c2", device_label="laptop", notify=_noop_notify,
    )
    assert acquired is True


# ── release_all_by_connection ──────────────────────────────────────────


async def test_release_all_by_connection(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="d", notify=_noop_notify,
    )
    await lm.try_acquire(
        session_id="s2", connection_id="c1", device_label="d", notify=_noop_notify,
    )
    await lm.try_acquire(
        session_id="s3", connection_id="c2", device_label="d", notify=_noop_notify,
    )
    released = await lm.release_all_by_connection("c1")
    assert sorted(released) == ["s1", "s2"]
    assert lm.active_locks == 1


# ── info ───────────────────────────────────────────────────────────────


async def test_info(lm: LockManager) -> None:
    assert lm.info("s1") is None
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="laptop", notify=_noop_notify,
    )
    entry = lm.info("s1")
    assert entry is not None
    assert entry.session_id == "s1"
    assert entry.device_label == "laptop"
