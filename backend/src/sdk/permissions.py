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
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[PermissionResultAllow | PermissionResultDeny]] = {}
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

    def cancel_all(self, reason: str = "Connection closed") -> None:
        """Deny every pending request — called on disconnect."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(PermissionResultDeny(message=reason))
        self._pending.clear()


_id_counter = 0


def _fallback_id() -> str:
    """Fallback id if SDK didn't provide tool_use_id."""
    global _id_counter
    _id_counter += 1
    return f"perm_{_id_counter}"
