"""Wrapper around `github-copilot-sdk`'s CopilotSession.

Mirrors worker-claude/sdk/session.py: one Session per active conversation,
bound to a project cwd, owns the SDK session lifecycle, dispatches SDK
events to the same WS message types the frontend already consumes from
worker-claude.

Event mapping (SDK SessionEvent.data type -> WS message type):

  AssistantMessageDeltaData  -> text_delta
  AssistantMessageData       -> (terminal text for the turn; emitted only when
                                  streaming was off, otherwise covered by deltas)
  AssistantReasoningDeltaData -> thinking
  AssistantReasoningData     -> (terminal reasoning; same caveat)
  ToolExecutionStartData     -> tool_call
  ToolExecutionCompleteData  -> tool_result
  SessionIdleData            -> result (turn end)
  SessionErrorData           -> error
  SystemMessageData          -> system

Permissions: registered via `on_permission_request` (Callable[[
PermissionRequest, dict[str,str]], Awaitable[PermissionRequestResult]]). The
broker bridges the SDK callback to our WS Allow/Deny round-trip.

Resume: `client.resume_session(session_id, ...)` reattaches to a session
whose state is persisted on disk under $COPILOT_HOME/session-state/<id>/.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog
from copilot import CopilotSession
from copilot.generated.session_events import (
    AbortData,
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantReasoningData,
    AssistantReasoningDeltaData,
    SessionErrorData,
    SessionEventData,
    SessionIdleData,
    SystemMessageData,
    ToolExecutionCompleteData,
    ToolExecutionStartData,
)
from copilot.session import PermissionRequest, PermissionRequestResult
import os as _os

from src.sdk.client import get_client
from src.sdk.permissions import PermissionBroker

logger = structlog.get_logger()


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class Session:
    """One Copilot SDK session bound to a project cwd + optional resume id."""

    def __init__(
        self,
        send: SendFn,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        auto_approve: bool = False,
        model: str | None = None,
    ) -> None:
        self._send = send
        self._cwd = cwd
        self._session_id = resume_session_id or session_id or str(uuid.uuid4())
        self._resume = resume_session_id
        self._model = model
        self._copilot_session: CopilotSession | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._permissions = PermissionBroker()
        self._permissions.set_auto_approve(auto_approve)
        self._stream_enabled = True
        self._busy = False
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._turn_started_at: float | None = None
        self._is_parked = False

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
        if self._copilot_session is not None:
            return
        client = await get_client()
        kwargs: dict[str, Any] = {
            "on_permission_request": self._on_permission_request,
            "on_user_input_request": self._on_user_input_request,
            "working_directory": self._cwd,
            "streaming": True,
        }
        if self._model:
            kwargs["model"] = self._model
        try:
            if self._resume:
                self._copilot_session = await client.resume_session(
                    self._resume, **kwargs
                )
            else:
                kwargs["session_id"] = self._session_id
                self._copilot_session = await client.create_session(**kwargs)
        except Exception as e:
            logger.error("copilot_session_start_failed", error=str(e), exc_info=True)
            await self._send({
                "type": "error",
                "payload": {"code": "session_start_failed", "message": str(e)},
            })
            return

        # Bridge SDK events into our async send loop. on() is sync; the
        # handler schedules an asyncio task per event.
        self._unsubscribe = self._copilot_session.on(self._on_sdk_event)

        # CopilotSession picks the session_id; sync ours to whatever it ended
        # up with so the frontend sees a consistent id (resume reuses, new
        # create may differ from our placeholder UUID).
        actual_id = getattr(self._copilot_session, "session_id", None)
        if isinstance(actual_id, str) and actual_id:
            self._session_id = actual_id

        logger.info(
            "copilot_session_started",
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
        """Disconnect from the SDK while preserving on-disk state."""
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        self._permissions.cancel_all("Session stopped")
        if self._copilot_session is not None:
            try:
                await self._copilot_session.disconnect()
            except Exception as e:
                logger.warning("copilot_disconnect_error", error=str(e))
            self._copilot_session = None
        logger.info("copilot_session_stopped", session_id=self._session_id)

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        """Send a prompt; events arrive asynchronously via on() callback."""
        if self._copilot_session is None:
            await self._send({
                "type": "error",
                "payload": {"code": "no_session", "message": "Session not started"},
            })
            return
        if self._busy:
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
        self._stream_enabled = stream
        self._busy = True
        self._idle_event.clear()
        self._turn_started_at = time.monotonic()
        # NOTE: images / attachments mapping to Copilot's Attachment shape is
        # left for a follow-up; text-only is enough for the phase-4 happy path.
        try:
            await self._copilot_session.send(text)
        except Exception as e:
            logger.error("copilot_send_failed", error=str(e), exc_info=True)
            self._busy = False
            self._idle_event.set()
            self._permissions.disarm_one_shot()
            await self._send({
                "type": "error",
                "payload": {"code": "send_failed", "message": str(e)},
            })

    async def interrupt(self) -> None:
        if self._copilot_session is None:
            return
        try:
            await self._copilot_session.abort()
        except Exception as e:
            logger.warning("copilot_abort_error", error=str(e))

    def set_auto_approve(self, value: bool) -> None:
        self._permissions.set_auto_approve(value)

    async def set_model(self, model: str | None) -> None:
        if self._copilot_session is None or not model:
            return
        try:
            await self._copilot_session.set_model(model)
            self._model = model
            logger.info("copilot_model_changed", model=model, session_id=self._session_id)
        except Exception as e:
            logger.warning("copilot_set_model_failed", error=str(e))
            await self._send({
                "type": "error",
                "payload": {"code": "set_model_failed", "message": str(e)},
            })

    def rebind(self, send: SendFn, *, parked: bool = False) -> None:
        self._send = send
        self._is_parked = parked

    async def notify_reconnected(self) -> None:
        await self._send({
            "type": "session_started",
            "payload": {
                "session_id": self._session_id,
                "cwd": self._cwd,
                "resumed": True,
                "auto_approve": self._permissions.is_auto_approve,
                "is_busy": self._busy,
            },
        })
        await self._permissions.resend_pending_user_inputs(self._send)

    # ── SDK callbacks ──────────────────────────────────────────────────────

    async def _on_permission_request(
        self, request: PermissionRequest, invocation: dict[str, str]
    ) -> PermissionRequestResult:
        """Forward Copilot's permission decision to our PermissionBroker."""
        return await self._permissions.request(self._send, self._session_id, request)

    async def _on_user_input_request(self, request: Any, invocation: dict[str, str]) -> Any:
        """Forward Copilot's free-text input prompt to the frontend.

        SDK shape: request has `prompt` and optional `options`/`choices`. We
        normalize to our `user_input_request` payload (list of questions).
        Returns the SDK's UserInputCompletedData-like dict, or {"answer": ""}
        on cancellation.
        """
        prompt_text = getattr(request, "prompt", None) or getattr(request, "message", None) or ""
        options_raw = getattr(request, "options", None) or getattr(request, "choices", None) or []
        options: list[str] = []
        for opt in options_raw if isinstance(options_raw, list) else []:
            if isinstance(opt, dict):
                options.append(opt.get("label") or opt.get("value") or str(opt))
            else:
                options.append(str(opt))
        tool_use_id = getattr(request, "request_id", None) or f"input_{id(request)}"
        answers = await self._permissions.request_user_input(
            self._send,
            self._session_id,
            tool_use_id,
            [{"question": prompt_text, "options": options}],
        )
        return {"answer": answers[0] if answers else ""}

    def _on_sdk_event(self, event: Any) -> None:
        """Sync entrypoint for SDK events. Schedules async dispatch."""
        try:
            asyncio.create_task(self._dispatch_event(event))
        except RuntimeError:
            # No running loop (unlikely - SDK runs in our loop). Drop silently.
            logger.warning("copilot_event_dropped_no_loop", event_type=getattr(event, "type", "?"))

    async def _dispatch_event(self, event: Any) -> None:
        data: SessionEventData | None = getattr(event, "data", None)
        # Temporary debug: log every event class we see so we can confirm
        # which tool lifecycle events the SDK actually emits in practice
        # (ToolExecutionComplete vs ToolExecutionPartialResult vs other).
        # Remove once the dispatcher is verified end-to-end.
        logger.info(
            "copilot_event",
            session_id=self._session_id,
            event_type=getattr(event, "type", None),
            data_class=type(data).__name__ if data is not None else None,
        )
        if data is None:
            return

        # Streaming-vs-block rule (matches worker-claude): per-prompt the
        # frontend sets `stream` (bool). When True, forward every Copilot
        # delta as a small text_delta and SUPPRESS the terminal
        # AssistantMessageData (otherwise the consolidated text would arrive
        # AFTER the deltas and the UI would render it as a duplicate). When
        # False, suppress deltas and emit only the consolidated text at the
        # end of the turn so the user sees one block per assistant message.

        if isinstance(data, AssistantMessageDeltaData):
            if not self._stream_enabled:
                return
            text = getattr(data, "delta_content", "") or ""
            if text:
                await self._send({
                    "type": "text_delta",
                    "payload": {"session_id": self._session_id, "text": text},
                })
            return

        if isinstance(data, AssistantMessageData):
            if self._stream_enabled:
                return
            text = getattr(data, "content", "") or ""
            if text:
                await self._send({
                    "type": "text_delta",
                    "payload": {"session_id": self._session_id, "text": text},
                })
            return

        if isinstance(data, AssistantReasoningDeltaData):
            if not self._stream_enabled:
                return
            text = getattr(data, "delta_content", "") or ""
            if text:
                await self._send({
                    "type": "thinking",
                    "payload": {"session_id": self._session_id, "text": text},
                })
            return

        if isinstance(data, AssistantReasoningData):
            if self._stream_enabled:
                return
            text = getattr(data, "content", "") or ""
            if text:
                await self._send({
                    "type": "thinking",
                    "payload": {"session_id": self._session_id, "text": text},
                })
            return

        if isinstance(data, ToolExecutionStartData):
            await self._send({
                "type": "tool_call",
                "payload": {
                    "session_id": self._session_id,
                    "tool_use_id": getattr(data, "tool_call_id", ""),
                    "name": getattr(data, "tool_name", "") or getattr(data, "mcp_tool_name", "") or "tool",
                    "input": getattr(data, "arguments", None) or {},
                },
            })
            return

        if isinstance(data, ToolExecutionCompleteData):
            raw_result = getattr(data, "result", None)
            raw_error = getattr(data, "error", None)
            # Some tools return rich pydantic/dataclass objects; fall back to
            # str() to guarantee the payload is JSON-serializable (otherwise
            # send_json raises and the message vanishes, leaving the UI stuck
            # at "running" forever).
            try:
                content: Any = raw_result if raw_result is not None else raw_error
                if content is not None and not isinstance(content, (str, int, float, bool, list, dict)):
                    content = str(content)
            except Exception:
                content = str(raw_result or raw_error)
            await self._send({
                "type": "tool_result",
                "payload": {
                    "session_id": self._session_id,
                    "tool_use_id": getattr(data, "tool_call_id", ""),
                    "content": content,
                    "is_error": not bool(getattr(data, "success", True)),
                },
            })
            return

        if isinstance(data, SessionIdleData):
            duration_ms = 0
            if self._turn_started_at is not None:
                duration_ms = int((time.monotonic() - self._turn_started_at) * 1000)
                self._turn_started_at = None
            aborted = bool(getattr(data, "aborted", False))
            self._permissions.disarm_one_shot()
            self._busy = False
            self._idle_event.set()
            await self._emit_push_notify(
                title="Copilot finished",
                body=f"Task completed in {self._cwd or 'background'}",
            )
            await self._send({
                "type": "result",
                "payload": {
                    "session_id": self._session_id,
                    "subtype": "aborted" if aborted else "success",
                    "duration_ms": duration_ms,
                    "total_cost_usd": 0.0,
                    "num_turns": 1,
                    "is_error": aborted,
                },
            })
            return

        if isinstance(data, AbortData):
            # Aborts also surface a SessionIdleData with aborted=True; treat
            # AbortData on its own as informational.
            return

        if isinstance(data, SessionErrorData):
            await self._send({
                "type": "error",
                "payload": {
                    "code": getattr(data, "error_type", "copilot_error") or "copilot_error",
                    "message": getattr(data, "message", "Copilot session error"),
                },
            })
            return

        if isinstance(data, SystemMessageData):
            await self._send({
                "type": "system",
                "payload": {
                    "session_id": self._session_id,
                    "subtype": getattr(data, "kind", "info") or "info",
                    "data": {"text": getattr(data, "content", "")},
                },
            })
            return

        # Other event types (subagent.*, mcp.*, command.*, session.*, etc.)
        # are not needed for the basic chat loop; ignore silently. Phase 4b
        # can selectively surface them as `task_event` / `system` for parity
        # with worker-claude's Monitor/TaskCreate handling.

    async def _emit_push_notify(self, *, title: str, body: str) -> None:
        """Out-of-band push notification, identical to worker-claude."""
        hub_url = _os.environ.get("HUB_URL", "").rstrip("/")
        secret = _os.environ.get("WORKER_SECRET", "")
        if not hub_url or not secret:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{hub_url}/api/internal/push",
                    json={"title": title, "body": body},
                    headers={"X-Worker-Secret": secret},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "push_notify_http_non_200",
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
        except Exception as e:
            logger.warning("push_notify_http_failed", error=str(e))
