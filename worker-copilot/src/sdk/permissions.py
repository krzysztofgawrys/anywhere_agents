"""Per-session permission flow for worker-copilot.

The github-copilot-sdk invokes `on_permission_request(request, invocation)`
for every tool that needs user approval (when not auto-approved). We forward
the request as a `permission_request` WS message to the frontend and block
on a future until the user clicks Allow / Deny, then return the appropriate
PermissionRequestResult to the SDK.

Auto-approve: when armed (per-project flag or per-prompt one-shot), we skip
the WS round-trip and return approve-once immediately.

Same lifecycle pattern as worker-claude/sdk/permissions.py:
- cancel_permissions(reason): cancel pending allow/deny on WS disconnect so
  the SDK loop doesn't block forever; user_input futures are kept alive so
  questions survive reconnect.
- cancel_all: cancel everything on full teardown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from copilot.session import PermissionRequest, PermissionRequestResult

logger = structlog.get_logger()


# PermissionRequestResultKind values per SDK 0.3.0:
#   "approve-once" | "reject" | "user-not-available" | "no-result"
_APPROVE = "approve-once"
_REJECT = "reject"
_USER_GONE = "user-not-available"


class PermissionBroker:
    """Manages pending permission requests for one Copilot session."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[PermissionRequestResult]] = {}
        # Pending user_input futures (Copilot's on_user_input_request callback).
        # Resolves to a list of answers, one per question.
        self._user_input_pending: dict[str, asyncio.Future[list[str]]] = {}
        self._user_input_meta: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        self._auto_approve: bool = False
        self._auto_approve_once: bool = False

    def set_auto_approve(self, value: bool) -> None:
        self._auto_approve = value

    def arm_one_shot(self) -> None:
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
        request: PermissionRequest,
    ) -> PermissionRequestResult:
        """Ask the WS client (or auto-approve) for permission to run a tool."""
        if self.is_auto_approve:
            return PermissionRequestResult(kind=_APPROVE)

        # SDK identifies the tool call by tool_call_id; fall back to id() if
        # the SDK didn't fill it (shouldn't happen but be defensive).
        tool_use_id = request.tool_call_id or f"perm_{id(request)}"
        loop = asyncio.get_event_loop()
        future: asyncio.Future[PermissionRequestResult] = loop.create_future()
        self._pending[tool_use_id] = future

        # Flatten the rich PermissionRequest object to the same WS payload the
        # frontend already renders for worker-claude (name + input + description).
        tool_input = (
            request.tool_args
            if request.tool_args is not None
            else (request.args if request.args is not None else {})
        )
        description = (
            request.tool_description
            or request.intention
            or request.reason
            or None
        )
        await send({
            "type": "permission_request",
            "payload": {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "name": request.tool_name or request.tool_title or "tool",
                "input": tool_input,
                "description": description,
            },
        })

        try:
            return await future
        finally:
            self._pending.pop(tool_use_id, None)

    def resolve(
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool:
        """Resolve a pending permission. Returns True if it matched.

        Uniform (allow, reason) signature shared with other workers
        (see worker_shared.sdk.base.PermissionsProtocol). `reason` is
        currently unused on this side - the github-copilot-sdk's
        PermissionRequestResult doesn't have a free-text deny message
        field. Kept for protocol parity.
        """
        del reason  # unused on copilot side
        fut = self._pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        fut.set_result(PermissionRequestResult(kind=_APPROVE if allow else _REJECT))
        return True

    async def request_user_input(
        self,
        send: Any,
        session_id: str,
        tool_use_id: str,
        questions: list[dict[str, Any]],
    ) -> list[str]:
        """Ask the WS client for free-text answer(s), block until they arrive."""
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

    def resolve_user_input(self, tool_use_id: str, answers: list[str]) -> bool:
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
        """Reject pending allow/deny so the SDK loop can continue."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(PermissionRequestResult(kind=_USER_GONE))
        self._pending.clear()

    def cancel_all(self, reason: str = "Connection closed") -> None:
        self.cancel_permissions(reason)
        for tool_use_id, fut in list(self._user_input_pending.items()):
            if fut.done():
                continue
            meta = self._user_input_meta.get(tool_use_id)
            expected = len(meta[1]) if meta else 1
            fut.set_result([""] * expected)
        self._user_input_pending.clear()
        self._user_input_meta.clear()
