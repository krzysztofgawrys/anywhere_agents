"""Per-session permission flow for worker-codex.

The openai-codex-sdk currently exposes Codex's CLI-side approval policy
through configuration rather than per-tool-call programmatic callbacks
(unlike claude_agent_sdk's `can_use_tool` or github-copilot-sdk's
`on_permission_request`). The SDK runs Codex with whatever approval
mode the user configured; intermediate "Codex wants to do X" prompts
do not surface to us programmatically.

That means our PermissionBroker here is a stub for protocol parity:
- `is_auto_approve` / `set_auto_approve` are honored (frontend can
  still flip the per-project flag and we record it).
- `resolve()` always returns False (no pending requests to resolve).
- `request_user_input()` is left as a no-op because Codex doesn't
  emit free-text questions through the SDK event stream we observe.

Practical effect: when running through worker-codex, all approval
gating happens inside Codex CLI itself per the user's configured
approval mode (`~/.codex/config.json` -> approval policy). The
frontend's permission UI never lights up. This is the documented
limitation of the current openai-codex-sdk; if a future SDK release
exposes a per-tool-call callback, plumb it in here against the same
`PermissionsProtocol` shape.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger()


class PermissionBroker:
    """Minimum implementation of PermissionsProtocol for worker-codex."""

    def __init__(self) -> None:
        self._auto_approve: bool = False
        self._auto_approve_once: bool = False
        # User-input futures kept for future SDK support and so
        # resend_pending_user_inputs() has a real (empty) dict to walk.
        self._user_input_pending: dict[str, asyncio.Future[list[str]]] = {}
        self._user_input_meta: dict[str, tuple[str, list[dict[str, Any]]]] = {}

    def set_auto_approve(self, value: bool) -> None:
        self._auto_approve = value

    def arm_one_shot(self) -> None:
        self._auto_approve_once = True

    def disarm_one_shot(self) -> None:
        self._auto_approve_once = False

    @property
    def is_auto_approve(self) -> bool:
        return self._auto_approve or self._auto_approve_once

    def resolve(
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool:
        """No-op: worker-codex doesn't surface programmatic approvals.

        Returns False so the WS handler can reply `no_pending` to a
        stray approve_tool / deny_tool. See module docstring.
        """
        del tool_use_id, allow, reason
        return False

    def resolve_user_input(
        self, tool_use_id: str, answers: list[str]
    ) -> bool:
        """Resolve a pending user_input_request, if any."""
        fut = self._user_input_pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        meta = self._user_input_meta.get(tool_use_id)
        expected = len(meta[1]) if meta else len(answers)
        normalized = list(answers[:expected])
        while len(normalized) < expected:
            normalized.append("")
        fut.set_result(normalized)
        return True

    async def request_user_input(
        self,
        send: Any,
        session_id: str,
        tool_use_id: str,
        questions: list[dict[str, Any]],
    ) -> list[str]:
        """Reserved for future SDK-level user input integration."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[list[str]] = loop.create_future()
        self._user_input_pending[tool_use_id] = future
        self._user_input_meta[tool_use_id] = (session_id, questions)
        await send({
            "type": "user_input_request",
            "payload": {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "questions": questions,
            },
        })
        try:
            return await future
        except asyncio.CancelledError:
            return [""] * len(questions)
        finally:
            self._user_input_pending.pop(tool_use_id, None)
            self._user_input_meta.pop(tool_use_id, None)

    async def resend_pending_user_inputs(self, send: Any) -> None:
        for tool_use_id, fut in list(self._user_input_pending.items()):
            if fut.done():
                continue
            meta = self._user_input_meta.get(tool_use_id)
            if meta is None:
                continue
            session_id, questions = meta
            await send({
                "type": "user_input_request",
                "payload": {
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "questions": questions,
                },
            })

    def cancel_permissions(self, reason: str = "Connection closed") -> None:
        """No-op: no permission futures to cancel."""
        del reason

    def cancel_all(self, reason: str = "Connection closed") -> None:
        """Cancel pending user_input futures on session teardown."""
        del reason
        for tool_use_id, fut in list(self._user_input_pending.items()):
            if fut.done():
                continue
            meta = self._user_input_meta.get(tool_use_id)
            expected = len(meta[1]) if meta else 1
            fut.set_result([""] * expected)
        self._user_input_pending.clear()
        self._user_input_meta.clear()
