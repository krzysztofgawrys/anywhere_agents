"""Per-session permission flow.

When a session runs in `default` mode, every tool call goes through
`can_use_tool`. We send a `permission_request` to the WS client and wait for
`approve_tool` or `deny_tool`. Approval futures live in PermissionBroker.

When the session is in `bypassPermissions` mode (auto-approve), Claude SDK
doesn't invoke `can_use_tool` at all - we still pass one for safety but it's
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
    survive reconnect) but we keep *user_input* futures alive - the agent is
    blocked waiting for an answer and we can re-surface the question after
    reconnect via :meth:`resend_pending_user_inputs`.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[PermissionResultAllow | PermissionResultDeny]] = {}
        # Futures waiting for free-text user answers (AskUserQuestion etc.).
        # The future resolves to a list of answers, one per question in the
        # original request (AskUserQuestion can ask multiple questions at once).
        self._user_input_pending: dict[str, asyncio.Future[list[str]]] = {}
        # Metadata needed to re-send the question(s) after reconnect:
        # (session_id, [{question, options}, ...])
        self._user_input_meta: dict[str, tuple[str, list[dict[str, Any]]]] = {}
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
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool:
        """Resolve a pending request. Returns True if it matched.

        Uniform (allow, reason) signature shared with other workers
        (see worker_shared.sdk.base.PermissionsProtocol). The
        conversion to claude_agent_sdk's PermissionResult types
        happens here so the shared SessionManager doesn't need to
        import the SDK.
        """
        fut = self._pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        result: PermissionResultAllow | PermissionResultDeny = (
            PermissionResultAllow()
            if allow
            else PermissionResultDeny(message=reason or "Denied by user")
        )
        fut.set_result(result)
        return True

    async def request_user_input(
        self,
        send: Any,
        session_id: str,
        tool_use_id: str,
        questions: list[dict[str, Any]],
    ) -> list[str]:
        """Ask the WS client for free-text answer(s) and block until they arrive.

        ``questions`` is a list of ``{question: str, options: list[str]}`` dicts -
        AskUserQuestion can pose multiple distinct questions in a single tool
        call, each with its own answer.

        Sends a ``user_input_request`` message and waits for a matching
        ``user_input_response`` resolved via :meth:`resolve_user_input`.
        Returns the list of user answers (one per question, in order). On
        cancellation returns a list of empty strings of matching length so
        callers can still zip with questions.

        Metadata (session_id, questions) is stored so the questions can
        be re-surfaced after a WS reconnect via :meth:`resend_pending_user_inputs`.
        """
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
        """Deliver user answers to a waiting :meth:`request_user_input` call.

        ``answers`` is one string per question (in order). If the list is
        shorter than the original questions, missing entries are treated as
        empty strings; extra entries are ignored.

        Returns True if a pending request with that id existed and was resolved.
        """
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
            session_id, questions = meta
            await send({
                "type": "user_input_request",
                "payload": {
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "questions": questions,
                },
            })
            logger.info("user_input_resent", tool_use_id=tool_use_id)

    def cancel_permissions(self, reason: str = "Connection closed") -> None:
        """Deny pending allow/deny permission requests.

        Called on WS disconnect so tool calls don't block forever.
        User-input futures are intentionally kept alive so questions survive
        reconnect - see :meth:`resend_pending_user_inputs`.
        """
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(PermissionResultDeny(message=reason))
        self._pending.clear()

    def cancel_all(self, reason: str = "Connection closed") -> None:
        """Deny every pending request - called on full session teardown."""
        self.cancel_permissions(reason)
        for tool_use_id, fut in list(self._user_input_pending.items()):
            if fut.done():
                continue
            meta = self._user_input_meta.get(tool_use_id)
            expected = len(meta[1]) if meta else 1
            fut.set_result([""] * expected)
        self._user_input_pending.clear()
        self._user_input_meta.clear()


_id_counter = 0


def _fallback_id() -> str:
    """Fallback id if SDK didn't provide tool_use_id."""
    global _id_counter
    _id_counter += 1
    return f"perm_{_id_counter}"
