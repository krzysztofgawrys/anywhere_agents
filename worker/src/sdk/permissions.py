"""Per-session permission flow.

When a session runs in `default` mode, every tool call goes through
`can_use_tool`. We send a `permission_request` to the WS client and wait for
`approve_tool` or `deny_tool`. Approval futures live in PermissionBroker.

When the session is in `bypassPermissions` mode (auto-approve), Claude SDK
doesn't invoke `can_use_tool` at all — we still pass one for safety but it's
unused.

Per-prompt override: the WS handler builds a Session with `auto_approve_once`
that flips the mode just for the next prompt's tool calls. Implemented by
toggling a flag on PermissionBroker.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = structlog.get_logger()


class PermissionBroker:
    """Manages pending permission requests for one session.

    The SDK's `can_use_tool` callback blocks on a Future until the WS client
    responds via approve_tool / deny_tool. If the connection drops, all
    pending futures are cancelled (treated as deny).

    User-input requests (AskUserQuestion et al.) are also brokered here via a
    separate dict so the WS handler can resolve them through a single entry
    point without coupling to the session internals.

    On WS disconnect we cancel *permission* futures (allow/deny decisions don't
    survive reconnect) but we keep *user_input* futures alive — the agent is
    blocked waiting for an answer and we can re-surface the question after
    reconnect via :meth:`resend_pending_user_inputs`.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[PermissionResultAllow | PermissionResultDeny]] = {}
        # Futures waiting for free-text user answers (AskUserQuestion etc.)
        self._user_input_pending: dict[str, asyncio.Future[str]] = {}
        # Metadata needed to re-send the question after reconnect
        self._user_input_meta: dict[str, tuple[str, str, list[str]]] = {}
        # Auto-approve mode flags
        self._auto_approve: bool = False
        self._auto_approve_once: bool = False  # consumed after first tool call

    def set_auto_approve(self, value: bool) -> None:
        self._auto_approve = value

    def arm_one_shot(self) -> None:
        """Approve every tool call in the next prompt, then clear."""
        self._auto_approve_once = True

    def disarm_one_shot(self) -> None:
        self._auto_approve_once = False

    @property
    def is_auto_approve(self) -> bool:
        return self._auto_approve or self._auto_approve_once

    async def request(
        self,
        send: Any,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Ask the WS client (or auto-approve) for permission to run the tool."""
        if self.is_auto_approve:
            return PermissionResultAllow()

        tool_use_id = ctx.tool_use_id or _fallback_id()
        loop = asyncio.get_event_loop()
        future: asyncio.Future[PermissionResultAllow | PermissionResultDeny] = loop.create_future()
        self._pending[tool_use_id] = future

        await send({
            "type": "permission_request",
            "payload": {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "name": tool_name,
                "input": tool_input,
                "description": ctx.description,
            },
        })

        try:
            result = await future
            return result
        finally:
            self._pending.pop(tool_use_id, None)

    def resolve(
        self,
        tool_use_id: str,
        result: PermissionResultAllow | PermissionResultDeny,
    ) -> bool:
        """Resolve a pending request. Returns True if it matched."""
        fut = self._pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        fut.set_result(result)
        return True

    async def request_user_input(
        self,
        send: Any,
        session_id: str,
        tool_use_id: str,
        question: str,
        options: list[str],
    ) -> str:
        """Ask the WS client for a free-text answer and block until it arrives.

        Sends a ``user_input_request`` message and waits for a matching
        ``user_input_response`` resolved via :meth:`resolve_user_input`.
        Returns the user's answer string (empty string on cancellation).

        Metadata (session_id, question, options) is stored so the question can
        be re-surfaced after a WS reconnect via :meth:`resend_pending_user_inputs`.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._user_input_pending[tool_use_id] = future
        self._user_input_meta[tool_use_id] = (session_id, question, options)

        await send({
            "type": "user_input_request",
            "payload": {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "question": question,
                "options": options,
            },
        })

        try:
            return await future
        except asyncio.CancelledError:
            return ""
        finally:
            self._user_input_pending.pop(tool_use_id, None)
            self._user_input_meta.pop(tool_use_id, None)

    def resolve_user_input(self, tool_use_id: str, answer: str) -> bool:
        """Deliver a user answer to a waiting :meth:`request_user_input` call.

        Returns True if a pending request with that id existed and was resolved.
        """
        fut = self._user_input_pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    async def resend_pending_user_inputs(self, send: Any) -> None:
        """Re-send all still-pending user_input_request messages to a new WS client.

        Called after a session is reclaimed from the registry so the reconnecting
        client sees questions the agent asked while the browser was closed.
        """
        for tool_use_id, fut in list(self._user_input_pending.items()):
            if fut.done():
                continue
            meta = self._user_input_meta.get(tool_use_id)
            if meta is None:
                continue
            session_id, question, options = meta
            await send({
                "type": "user_input_request",
                "payload": {
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "question": question,
                    "options": options,
                },
            })
            logger.info("user_input_resent", tool_use_id=tool_use_id)

    def cancel_permissions(self, reason: str = "Connection closed") -> None:
        """Deny pending allow/deny permission requests.

        Called on WS disconnect so tool calls don't block forever.
        User-input futures are intentionally kept alive so questions survive
        reconnect — see :meth:`resend_pending_user_inputs`.
        """
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(PermissionResultDeny(message=reason))
        self._pending.clear()

    def cancel_all(self, reason: str = "Connection closed") -> None:
        """Deny every pending request — called on full session teardown."""
        self.cancel_permissions(reason)
        for fut in list(self._user_input_pending.values()):
            if not fut.done():
                fut.set_result("")
        self._user_input_pending.clear()
        self._user_input_meta.clear()


_id_counter = 0


def _fallback_id() -> str:
    """Fallback id if SDK didn't provide tool_use_id."""
    global _id_counter
    _id_counter += 1
    return f"perm_{_id_counter}"
