"""Tests for LockManager - Phase 4."""

from __future__ import annotations

from typing import Any

import pytest

from src.locks.manager import LockManager


@pytest.fixture
def lm() -> LockManager:
    return LockManager()


async def _noop(_msg: dict[str, Any]) -> None:
    pass


async def test_acquire_succeeds_when_free(lm: LockManager) -> None:
    acquired, existing = await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=_noop
    )
    assert acquired is True
    assert existing is None
    assert lm.active_locks == 1


async def test_acquire_fails_when_held_by_other(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=_noop
    )
    acquired, existing = await lm.try_acquire(
        session_id="s1", connection_id="c2", device_label="laptop", notify=_noop
    )
    assert acquired is False
    assert existing is not None
    assert existing.connection_id == "c1"
    assert existing.device_label == "phone"


async def test_reacquire_by_same_connection_succeeds(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=_noop
    )
    acquired, _ = await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=_noop
    )
    assert acquired is True


async def test_force_takeover_notifies_previous_owner(lm: LockManager) -> None:
    received: list[dict[str, Any]] = []

    async def notify_old(msg: dict[str, Any]) -> None:
        received.append(msg)

    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=notify_old
    )
    acquired, _ = await lm.try_acquire(
        session_id="s1", connection_id="c2", device_label="laptop",
        notify=_noop, force=True,
    )
    assert acquired is True
    assert received and received[0]["type"] == "lock_revoked"
    assert received[0]["payload"]["session_id"] == "s1"
    # New owner is c2
    assert lm.info("s1") is not None
    info = lm.info("s1")
    assert info is not None
    assert info.connection_id == "c2"


async def test_release_only_by_owner(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=_noop
    )
    # Wrong owner can't release
    assert await lm.release("s1", "c2") is False
    assert lm.active_locks == 1
    # Owner can
    assert await lm.release("s1", "c1") is True
    assert lm.active_locks == 0


async def test_release_all_by_connection(lm: LockManager) -> None:
    await lm.try_acquire(
        session_id="s1", connection_id="c1", device_label="phone", notify=_noop
    )
    await lm.try_acquire(
        session_id="s2", connection_id="c1", device_label="phone", notify=_noop
    )
    await lm.try_acquire(
        session_id="s3", connection_id="c2", device_label="laptop", notify=_noop
    )
    released = await lm.release_all_by_connection("c1")
    assert sorted(released) == ["s1", "s2"]
    assert lm.active_locks == 1
    assert lm.info("s3") is not None
