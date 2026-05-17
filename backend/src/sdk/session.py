"""ClaudeSDKClient wrapper — one Session per active conversation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.sdk.permissions import PermissionBroker

logger = structlog.get_logger()


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class Session:
    """One Claude SDK session bound to a project cwd + optional resume id."""

    def __init__(
        self,
        send: SendFn,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        auto_approve: bool = False,
    ) -> None:
        self._send = send
        self._cwd = cwd
        self._session_id = resume_session_id or session_id or str(uuid.uuid4())
        self._resume = resume_session_id
        self._client: ClaudeSDKClient | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._permissions = PermissionBroker()
        self._permissions.set_auto_approve(auto_approve)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cwd(self) -> str | None:
        return self._cwd

    @property
    def permissions(self) -> PermissionBroker:
        return self._permissions

    async def start(self) -> None:
        if self._client is not None:
            return

        # We always use 'default' permission mode and rely on can_use_tool to
        # gate or auto-approve. This gives us per-prompt override capability.
        options = ClaudeAgentOptions(
            cwd=self._cwd,
            setting_sources=["user", "project"],
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            resume=self._resume,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        logger.info(
            "sdk_session_started",
            session_id=self._session_id,
            cwd=self._cwd,
            resumed=bool(self._resume),
            auto_approve=self._permissions.is_auto_approve,
        )

        await self._send({
            "type": "session_started",
            "payload": {
                "session_id": self._session_id,
                "cwd": self._cwd,
                "resumed": bool(self._resume),
                "auto_approve": self._permissions.is_auto_approve,
            },
        })

    async def stop(self) -> None:
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel any pending permission requests so the SDK loop unblocks
        self._permissions.cancel_all("Session stopped")

        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.warning("sdk_disconnect_error", error=str(e))
            self._client = None
        logger.info("sdk_session_stopped", session_id=self._session_id)

    async def send_prompt(self, text: str, *, auto_approve_once: bool = False) -> None:
        if self._client is None:
            await self._send({
                "type": "error",
                "payload": {"code": "no_session", "message": "Session not started"},
            })
            return

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

            if auto_approve_once:
                self._permissions.arm_one_shot()

            await self._client.query(text, session_id=self._session_id)
            self._stream_task = asyncio.create_task(self._consume_stream())

    async def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception as e:
            logger.warning("sdk_interrupt_error", error=str(e))

    def set_auto_approve(self, value: bool) -> None:
        self._permissions.set_auto_approve(value)

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK callback — invoked for every tool call when permission_mode='default'."""
        return await self._permissions.request(
            self._send, self._session_id, tool_name, tool_input, ctx
        )

    async def _consume_stream(self) -> None:
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
        finally:
            # One-shot auto-approve only lasts until the prompt's result arrives
            self._permissions.disarm_one_shot()

    async def _dispatch(self, msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    await self._send({
                        "type": "text_delta",
                        "payload": {"session_id": self._session_id, "text": block.text},
                    })
                elif isinstance(block, ThinkingBlock):
                    await self._send({
                        "type": "thinking",
                        "payload": {"session_id": self._session_id, "text": block.thinking},
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
    if isinstance(content, list):
        return content
    return []


def _serialize_tool_content(content: Any) -> Any:
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
