"""Tests for worker_shared.sdk.registry - session parking registry."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker_shared.sdk.registry import SessionRegistry


def _make_session(session_id: str = "s1") -> MagicMock:
    """Create a mock that satisfies the registry's Session protocol."""
    s = MagicMock()
    s.session_id = session_id
    s.permissions = MagicMock()
    s.permissions.cancel_permissions = MagicMock()
    s.rebind = MagicMock()
    s.stop = AsyncMock()
    return s


@pytest.fixture
def reg() -> SessionRegistry:
    return SessionRegistry()


# ── Park / take basics ─────────────────────────────────────────────────


async def test_park_and_take(reg: SessionRegistry) -> None:
    s = _make_session("s1")
    reg.park(s)
    assert reg.has("s1")
    assert reg.parked_count == 1

    taken = reg.take("s1")
    assert taken is s
    assert not reg.has("s1")
    assert reg.parked_count == 0


def test_take_nonexistent(reg: SessionRegistry) -> None:
    assert reg.take("nope") is None


async def test_park_replaces_existing(reg: SessionRegistry) -> None:
    s1 = _make_session("s1")
    s2 = _make_session("s1")
    reg.park(s1)
    reg.park(s2)
    assert reg.parked_count == 1
    taken = reg.take("s1")
    assert taken is s2


# ── Park side effects ──────────────────────────────────────────────────


async def test_park_rebinds_with_noop_and_cancels_permissions(reg: SessionRegistry) -> None:
    s = _make_session("s1")
    reg.park(s)
    s.rebind.assert_called_once()
    _, kwargs = s.rebind.call_args
    assert kwargs.get("parked") is True
    s.permissions.cancel_permissions.assert_called_once()


# ── Expiry ─────────────────────────────────────────────────────────────


async def test_expiry_stops_session(reg: SessionRegistry) -> None:
    """Parked session is stopped after the TTL expires."""
    s = _make_session("s1")

    # Patch sleep so the expiry fires immediately instead of waiting 1 hour
    with patch("worker_shared.sdk.registry.DETACH_TTL", 0):
        reg.park(s)
        # Let the event loop process the create_task + sleep(0)
        await asyncio.sleep(0.05)

    s.stop.assert_awaited_once()
    assert not reg.has("s1")


async def test_take_cancels_expiry(reg: SessionRegistry) -> None:
    s = _make_session("s1")
    reg.park(s)
    task = reg._expiry_tasks.get("s1")
    assert task is not None

    reg.take("s1")
    assert "s1" not in reg._expiry_tasks


# ── stop_all ───────────────────────────────────────────────────────────


async def test_stop_all(reg: SessionRegistry) -> None:
    s1 = _make_session("s1")
    s2 = _make_session("s2")
    reg.park(s1)
    reg.park(s2)

    await reg.stop_all()
    s1.stop.assert_awaited_once()
    s2.stop.assert_awaited_once()
    assert reg.parked_count == 0


# ── Dict-like helpers ──────────────────────────────────────────────────


async def test_contains_and_len(reg: SessionRegistry) -> None:
    assert "s1" not in reg
    assert len(reg) == 0

    s = _make_session("s1")
    reg.park(s)
    assert "s1" in reg
    assert len(reg) == 1
