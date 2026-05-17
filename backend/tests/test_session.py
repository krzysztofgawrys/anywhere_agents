"""Tests for SDK session wrapper — Phase 2."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.sdk.session import Session


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient — records calls, replays fixed messages."""

    def __init__(self, messages: list[Any] | None = None) -> None:
        self._messages = messages or []
        self.connected = False
        self.disconnected = False
        self.queries: list[tuple[str, str]] = []
        self.interrupts = 0

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append((prompt, session_id))

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def receive_response(self) -> AsyncIterator[Any]:
        for m in self._messages:
            yield m


@pytest.fixture
def collected_messages() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def send_fn(collected_messages: list[dict[str, Any]]) -> Any:
    async def _send(msg: dict[str, Any]) -> None:
        collected_messages.append(msg)
    return _send


async def test_start_emits_session_started(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    fake = _FakeClient()
    with patch("src.sdk.session.ClaudeSDKClient", return_value=fake):
        session = Session(send=send_fn)
        await session.start()

    assert fake.connected is True
    assert any(m["type"] == "session_started" for m in collected_messages)


async def test_prompt_streams_text_delta_and_result(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    messages = [
        AssistantMessage(
            content=[TextBlock(text="Hello")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=1,
            session_id="default",
            total_cost_usd=0.0001,
        ),
    ]
    fake = _FakeClient(messages=messages)

    with patch("src.sdk.session.ClaudeSDKClient", return_value=fake):
        session = Session(send=send_fn)
        await session.start()
        await session.send_prompt("hi")
        # Wait for stream task to finish
        assert session._stream_task is not None
        await session._stream_task

    types = [m["type"] for m in collected_messages]
    assert "session_started" in types
    assert "text_delta" in types
    assert "result" in types

    text_msg = next(m for m in collected_messages if m["type"] == "text_delta")
    assert text_msg["payload"]["text"] == "Hello"

    assert fake.queries == [("hi", "default")]


async def test_prompt_streams_tool_call_and_result(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    messages = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"}),
            ],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="tu_1", content="file1\nfile2", is_error=False),
            ],
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=1,
            session_id="default",
            total_cost_usd=0.0001,
        ),
    ]
    fake = _FakeClient(messages=messages)

    with patch("src.sdk.session.ClaudeSDKClient", return_value=fake):
        session = Session(send=send_fn)
        await session.start()
        await session.send_prompt("ls files")
        assert session._stream_task is not None
        await session._stream_task

    tool_call = next(m for m in collected_messages if m["type"] == "tool_call")
    assert tool_call["payload"]["name"] == "Bash"
    assert tool_call["payload"]["tool_use_id"] == "tu_1"

    tool_result = next(m for m in collected_messages if m["type"] == "tool_result")
    assert tool_result["payload"]["tool_use_id"] == "tu_1"
    assert tool_result["payload"]["content"] == "file1\nfile2"
    assert tool_result["payload"]["is_error"] is False


async def test_interrupt_calls_sdk(send_fn: Any) -> None:
    fake = _FakeClient()
    with patch("src.sdk.session.ClaudeSDKClient", return_value=fake):
        session = Session(send=send_fn)
        await session.start()
        await session.interrupt()

    assert fake.interrupts == 1


async def test_stop_disconnects_sdk(send_fn: Any) -> None:
    fake = _FakeClient()
    with patch("src.sdk.session.ClaudeSDKClient", return_value=fake):
        session = Session(send=send_fn)
        await session.start()
        await session.stop()

    assert fake.disconnected is True


async def test_prompt_without_start_returns_error(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    session = Session(send=send_fn)
    await session.send_prompt("hi")
    assert any(
        m["type"] == "error" and m["payload"]["code"] == "no_session"
        for m in collected_messages
    )
