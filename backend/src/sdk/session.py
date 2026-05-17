"""ClaudeSDKClient wrapper — one session per WebSocket connection (Phase 2).

Phase 2 scope: single hardcoded session per connection, prompt → stream messages back.
Multi-session, project routing, locks etc. come later (Phases 3-4).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

logger = structlog.get_logger()


# Callback signature: receives a dict to send over WS
SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class Session:
    """One Claude SDK session bound to one WS connection.

    Lifecycle:
        session = Session(send_fn=...)
        await session.start()
        await session.send_prompt("hello")    # streams via send_fn
        await session.interrupt()             # cancel current stream
        await session.stop()
    """

    def __init__(
        self,
        send: SendFn,
        *,
        cwd: str | None = None,
        session_id: str = "default",
    ) -> None:
        self._send = send
        self._cwd = cwd
        self._session_id = session_id
        self._client: ClaudeSDKClient | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    async def start(self) -> None:
        """Connect to the SDK. Idempotent."""
        if self._client is not None:
            return

        options = ClaudeAgentOptions(
            cwd=self._cwd,
            setting_sources=["user", "project"],
            permission_mode="bypassPermissions",  # Phase 2: no approval flow yet
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        logger.info("sdk_session_started", session_id=self._session_id, cwd=self._cwd)

        await self._send({
            "type": "session_started",
            "payload": {"session_id": self._session_id, "history": []},
        })

    async def stop(self) -> None:
        """Disconnect from the SDK and cancel any in-flight stream."""
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.warning("sdk_disconnect_error", error=str(e))
            self._client = None
        logger.info("sdk_session_stopped", session_id=self._session_id)

    async def send_prompt(self, text: str) -> None:
        """Send a prompt and stream the response. Non-blocking — runs in a task."""
        if self._client is None:
            await self._send({
                "type": "error",
                "payload": {"code": "no_session", "message": "Session not started"},
            })
            return

        # Serialize prompts — one at a time per session
        async with self._lock:
            if self._stream_task and not self._stream_task.done():
                await self._send({
                    "type": "error",
                    "payload": {
                        "code": "busy",
                        "message": "Previous prompt still streaming, send interrupt first",
                    },
                })
                return

            await self._client.query(text, session_id=self._session_id)
            self._stream_task = asyncio.create_task(self._consume_stream())

    async def interrupt(self) -> None:
        """Interrupt the current stream."""
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception as e:
            logger.warning("sdk_interrupt_error", error=str(e))

    async def _consume_stream(self) -> None:
        """Read SDK messages and forward them to the WS client."""
        assert self._client is not None
        try:
            async for msg in self._client.receive_response():
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("sdk_stream_error", error=str(e), exc_info=True)
            await self._send({
                "type": "error",
                "payload": {"code": "sdk_stream_error", "message": str(e)},
            })

    async def _dispatch(self, msg: Any) -> None:
        """Convert SDK message → WS protocol message."""
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    await self._send({
                        "type": "text_delta",
                        "payload": {
                            "session_id": self._session_id,
                            "text": block.text,
                        },
                    })
                elif isinstance(block, ThinkingBlock):
                    await self._send({
                        "type": "thinking",
                        "payload": {
                            "session_id": self._session_id,
                            "text": block.thinking,
                        },
                    })
                elif isinstance(block, ToolUseBlock):
                    await self._send({
                        "type": "tool_call",
                        "payload": {
                            "session_id": self._session_id,
                            "tool_use_id": block.id,
                            "name": block.name,
                            "input": block.input,
                        },
                    })

        elif isinstance(msg, UserMessage):
            # UserMessage in stream = tool result echoed back
            for block in _iter_blocks(msg.content):
                if isinstance(block, ToolResultBlock):
                    await self._send({
                        "type": "tool_result",
                        "payload": {
                            "session_id": self._session_id,
                            "tool_use_id": block.tool_use_id,
                            "content": _serialize_tool_content(block.content),
                            "is_error": bool(block.is_error),
                        },
                    })

        elif isinstance(msg, SystemMessage):
            # System messages (init, model info) — surface subtype for debugging
            await self._send({
                "type": "system",
                "payload": {
                    "session_id": self._session_id,
                    "subtype": msg.subtype,
                    "data": msg.data,
                },
            })

        elif isinstance(msg, ResultMessage):
            await self._send({
                "type": "result",
                "payload": {
                    "session_id": self._session_id,
                    "subtype": msg.subtype,
                    "duration_ms": msg.duration_ms,
                    "total_cost_usd": msg.total_cost_usd,
                    "num_turns": msg.num_turns,
                    "is_error": msg.is_error,
                },
            })


def _iter_blocks(content: Any) -> AsyncIterator[Any] | list[Any]:
    """UserMessage.content may be a string or a list of blocks."""
    if isinstance(content, list):
        return content
    return []


def _serialize_tool_content(content: Any) -> Any:
    """Tool result content can be string or list of content blocks. Pass through
    primitives, dataclass-like blocks become dicts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if hasattr(block, "__dict__"):
                out.append({k: v for k, v in block.__dict__.items() if not k.startswith("_")})
            else:
                out.append(block)
        return out
    return content
