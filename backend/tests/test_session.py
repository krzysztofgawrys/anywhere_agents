"""Tests for SDK session wrapper - Phase 2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.sdk.session import Session


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient - records calls, replays fixed messages."""

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

    async def receive_messages(self) -> AsyncIterator[Any]:
        # Long-lived consumer: yield all queued messages, then park so the
        # stream task doesn't terminate (mirrors real SDK behaviour between
        # turns) - the test stops the session at teardown.
        for m in self._messages:
            yield m
        # Sleep forever; cancelled by Session.stop().
        await asyncio.Event().wait()


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
    """Text is streamed via StreamEvent content_block_delta in include_partial mode.

    The final AssistantMessage carries the assembled TextBlock but we ignore it
    to avoid duplication - only ToolUseBlock from AssistantMessage is forwarded.
    """
    messages = [
        StreamEvent(
            uuid="e1",
            session_id="default",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            },
            parent_tool_use_id=None,
        ),
        StreamEvent(
            uuid="e2",
            session_id="default",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            },
            parent_tool_use_id=None,
        ),
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
        session = Session(send=send_fn, session_id="fixed-id")
        await session.start()
        await session.send_prompt("hi", stream=True)
        assert session._stream_task is not None
        await asyncio.wait_for(session._idle_event.wait(), timeout=2.0)
        await session.stop()

    types = [m["type"] for m in collected_messages]
    assert "session_started" in types
    assert "result" in types

    # Two separate text_delta events, not one consolidated "Hello"
    deltas = [m for m in collected_messages if m["type"] == "text_delta"]
    assert [d["payload"]["text"] for d in deltas] == ["Hel", "lo"]

    assert fake.queries == [("hi", "fixed-id")]


async def test_prompt_without_stream_suppresses_deltas_emits_full_text(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    """stream=False: ignore StreamEvent deltas, emit whole TextBlock from AssistantMessage."""
    messages = [
        StreamEvent(
            uuid="e1",
            session_id="default",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            },
            parent_tool_use_id=None,
        ),
        StreamEvent(
            uuid="e2",
            session_id="default",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            },
            parent_tool_use_id=None,
        ),
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
        session = Session(send=send_fn, session_id="fixed-id")
        await session.start()
        await session.send_prompt("hi", stream=False)
        assert session._stream_task is not None
        await asyncio.wait_for(session._idle_event.wait(), timeout=2.0)
        await session.stop()

    deltas = [m for m in collected_messages if m["type"] == "text_delta"]
    # Exactly one delta with the full text, not two partial chunks
    assert len(deltas) == 1
    assert deltas[0]["payload"]["text"] == "Hello"


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
        await asyncio.wait_for(session._idle_event.wait(), timeout=2.0)
        await session.stop()

    tool_call = next(m for m in collected_messages if m["type"] == "tool_call")
    assert tool_call["payload"]["name"] == "Bash"
    assert tool_call["payload"]["tool_use_id"] == "tu_1"

    tool_result = next(m for m in collected_messages if m["type"] == "tool_result")
    assert tool_result["payload"]["tool_use_id"] == "tu_1"
    assert tool_result["payload"]["content"] == "file1\nfile2"
    assert tool_result["payload"]["is_error"] is False


async def test_task_notification_emits_task_event(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    """Monitor / TaskCreate task_* system messages stream out as `task_event`.

    Without this the UI sees a 'running…' tool call with no progress until the
    tool finally returns - symptom is "chat appears frozen during Monitor".
    """
    messages = [
        SystemMessage(
            subtype="task_started",
            data={
                "task_id": "blz0ay4b2",
                "description": "ECR push + auto-deploy for commit e7e20d7",
                "tool_use_id": "tu_monitor_1",
            },
        ),
        SystemMessage(
            subtype="task_notification",
            data={
                "task_id": "blz0ay4b2",
                "status": "completed",
                "summary": 'Monitor event: "ECR push + auto-deploy"',
                "tool_use_id": "tu_monitor_1",
            },
        ),
        SystemMessage(
            subtype="some_other_subtype",
            data={"foo": "bar"},
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
        session = Session(send=send_fn, session_id="sid")
        await session.start()
        await session.send_prompt("watch deploy")
        assert session._stream_task is not None
        await asyncio.wait_for(session._idle_event.wait(), timeout=2.0)
        await session.stop()

    task_events = [m for m in collected_messages if m["type"] == "task_event"]
    assert len(task_events) == 2

    started = task_events[0]["payload"]
    assert started["event_type"] == "started"
    assert started["task_id"] == "blz0ay4b2"
    assert started["description"] == "ECR push + auto-deploy for commit e7e20d7"
    assert started["tool_use_id"] == "tu_monitor_1"

    notification = task_events[1]["payload"]
    assert notification["event_type"] == "notification"
    assert notification["status"] == "completed"
    assert notification["summary"] == 'Monitor event: "ECR push + auto-deploy"'

    # Unknown subtypes still fall through to the generic `system` channel.
    systems = [m for m in collected_messages if m["type"] == "system"]
    assert len(systems) == 1
    assert systems[0]["payload"]["subtype"] == "some_other_subtype"


async def test_user_textblock_task_xml_emits_task_event(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    """CLI injects <task-notification> XML into user-message TextBlocks.

    The session must parse those out and surface as `task_event` rather than
    silently dropping them (which left the UI frozen during Monitor) and
    rather than letting them through as user text (which made them appear as
    blue user bubbles after a history reload).
    """
    notification_xml = (
        "<task-notification>\n"
        "<task-id>bx202339b</task-id>\n"
        "<tool-use-id>toolu_01ABC</tool-use-id>\n"
        "<status>completed</status>\n"
        '<summary>Monitor "demo" stream ended</summary>\n'
        "</task-notification>"
    )
    progress_xml = (
        "<task-notification>\n"
        "<task-id>bx202339b</task-id>\n"
        '<summary>Monitor event: "demo"</summary>\n'
        "<event>tick 1 at 00:39:07</event>\n"
        "</task-notification>"
    )
    messages = [
        UserMessage(
            content=[TextBlock(text=progress_xml)],
            parent_tool_use_id=None,
        ),
        UserMessage(
            content=[TextBlock(text=notification_xml)],
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
        session = Session(send=send_fn, session_id="sid")
        await session.start()
        await session.send_prompt("watch")
        await asyncio.wait_for(session._idle_event.wait(), timeout=2.0)
        await session.stop()

    task_events = [m for m in collected_messages if m["type"] == "task_event"]
    assert len(task_events) == 2

    first = task_events[0]["payload"]
    assert first["task_id"] == "bx202339b"
    assert first["summary"] == 'Monitor event: "demo"'
    assert first["description"] == "tick 1 at 00:39:07"

    second = task_events[1]["payload"]
    assert second["status"] == "completed"
    assert second["tool_use_id"] == "toolu_01ABC"
    assert second["summary"] == 'Monitor "demo" stream ended'


def test_parse_task_events_handles_mixed_text() -> None:
    """parse_task_events extracts every block, leaves non-task text alone."""
    from src.sdk.session import parse_task_events, strip_task_events

    text = (
        "some preamble\n"
        "<task-notification>\n"
        "<task-id>t1</task-id>\n"
        "<summary>one</summary>\n"
        "</task-notification>\n"
        "<task-started>\n"
        "<task-id>t2</task-id>\n"
        "<description>kicked off</description>\n"
        "</task-started>\n"
        "trailing\n"
    )
    events = parse_task_events(text)
    assert len(events) == 2
    assert events[0]["event_type"] == "notification"
    assert events[0]["task_id"] == "t1"
    assert events[1]["event_type"] == "started"
    assert events[1]["description"] == "kicked off"

    stripped = strip_task_events(text)
    assert "task-notification" not in stripped
    assert "task-started" not in stripped
    assert "preamble" in stripped
    assert "trailing" in stripped


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


async def test_multimodal_stream_builder() -> None:
    """The helper emits a user message with mixed text+image blocks."""
    from src.sdk.session import _build_multimodal_stream

    images = [
        {"media_type": "image/png", "data_b64": "AAAA"},
        {"media_type": "image/jpeg", "data_b64": "BBBB"},
        {"media_type": "text/html", "data_b64": "ignored"},  # filtered
        {"media_type": "image/png", "data_b64": ""},  # filtered (no data)
    ]
    collected = []
    async for msg in _build_multimodal_stream("hello", images, "sid"):
        collected.append(msg)

    assert len(collected) == 1
    m = collected[0]
    assert m["type"] == "user"
    assert m["session_id"] == "sid"
    content = m["message"]["content"]
    # text + 2 valid images
    assert content[0] == {"type": "text", "text": "hello"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"] == "AAAA"
    assert content[2]["source"]["media_type"] == "image/jpeg"
    assert len(content) == 3


async def test_send_prompt_with_images_uses_async_stream(
    send_fn: Any, collected_messages: list[dict[str, Any]]
) -> None:
    """When images are provided, .query receives an async iterable, not a string."""
    fake = _FakeClient()
    captured: list[Any] = []

    async def fake_query(prompt: Any, session_id: str = "default") -> None:
        captured.append((prompt, session_id))

    fake.query = fake_query  # type: ignore[method-assign]

    with patch("src.sdk.session.ClaudeSDKClient", return_value=fake):
        session = Session(send=send_fn, session_id="s")
        await session.start()
        await session.send_prompt(
            "look",
            images=[{"media_type": "image/png", "data_b64": "AAAA"}],
        )

    assert len(captured) == 1
    prompt, sid = captured[0]
    # Not a plain string - should be an async iterable
    assert not isinstance(prompt, str)
    # Drain it
    collected = []
    async for msg in prompt:
        collected.append(msg)
    assert len(collected) == 1
    blocks = collected[0]["message"]["content"]
    assert any(b["type"] == "image" for b in blocks)
