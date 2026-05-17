"""Tests for PermissionBroker — Phase 5."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.sdk.permissions import PermissionBroker


@dataclass
class _Ctx:
    """Minimal stand-in for ToolPermissionContext."""

    tool_use_id: str = "tu_1"
    description: str | None = None
    signal: Any = None
    display_name: str | None = None
    title: str | None = None
    blocked_path: str | None = None
    agent_id: str | None = None
    decision_reason: Any = None


@pytest.fixture
def broker() -> PermissionBroker:
    return PermissionBroker()


@pytest.fixture
def sent() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def send(sent: list[dict[str, Any]]) -> Any:
    async def _send(msg: dict[str, Any]) -> None:
        sent.append(msg)
    return _send


async def test_auto_approve_returns_allow_without_sending(
    broker: PermissionBroker, send: Any, sent: list[dict[str, Any]]
) -> None:
    broker.set_auto_approve(True)
    result = await broker.request(send, "sid", "Bash", {"command": "ls"}, _Ctx())
    assert isinstance(result, PermissionResultAllow)
    assert sent == []


async def test_one_shot_armed_overrides_default_deny(
    broker: PermissionBroker, send: Any
) -> None:
    broker.arm_one_shot()
    result = await broker.request(send, "sid", "Bash", {"command": "ls"}, _Ctx())
    assert isinstance(result, PermissionResultAllow)


async def test_request_emits_permission_request_and_blocks_until_resolved(
    broker: PermissionBroker, send: Any, sent: list[dict[str, Any]]
) -> None:
    ctx = _Ctx(tool_use_id="tu_abc")
    task = asyncio.create_task(
        broker.request(send, "sid", "Edit", {"file_path": "/x"}, ctx)
    )

    # Wait until the permission_request hit the wire
    for _ in range(50):
        await asyncio.sleep(0.01)
        if sent:
            break

    assert sent and sent[0]["type"] == "permission_request"
    assert sent[0]["payload"]["tool_use_id"] == "tu_abc"
    assert sent[0]["payload"]["name"] == "Edit"

    # Resolve with allow
    assert broker.resolve("tu_abc", PermissionResultAllow()) is True
    result = await task
    assert isinstance(result, PermissionResultAllow)


async def test_resolve_deny(
    broker: PermissionBroker, send: Any
) -> None:
    ctx = _Ctx(tool_use_id="tu_x")
    task = asyncio.create_task(
        broker.request(send, "sid", "Write", {"file_path": "/x"}, ctx)
    )
    await asyncio.sleep(0.02)
    broker.resolve("tu_x", PermissionResultDeny(message="no thanks"))
    result = await task
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "no thanks"


async def test_cancel_all_denies_pending(
    broker: PermissionBroker, send: Any
) -> None:
    task = asyncio.create_task(
        broker.request(send, "sid", "Bash", {"command": "ls"}, _Ctx(tool_use_id="tu_y"))
    )
    await asyncio.sleep(0.02)
    broker.cancel_all("bye")
    result = await task
    assert isinstance(result, PermissionResultDeny)
    assert "bye" in result.message


async def test_resolve_unknown_id_returns_false(broker: PermissionBroker) -> None:
    assert broker.resolve("does_not_exist", PermissionResultAllow()) is False


async def test_one_shot_disarm(broker: PermissionBroker) -> None:
    broker.arm_one_shot()
    assert broker.is_auto_approve is True
    broker.disarm_one_shot()
    assert broker.is_auto_approve is False
