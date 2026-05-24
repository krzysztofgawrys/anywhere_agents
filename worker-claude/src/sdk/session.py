"""ClaudeSDKClient wrapper — one Session per active conversation."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.sdk.permissions import PermissionBroker, _fallback_id

logger = structlog.get_logger()


SendFn = Callable[[dict[str, Any]], Awaitable[None]]

# Tool names that represent the agent asking the user a question.
# When the agent calls one of these, we surface a question UI instead of the
# generic Allow / Deny permission prompt.
_USER_INPUT_TOOLS: frozenset[str] = frozenset({
    "AskUserQuestion",
    "ask_followup_question",
    "ask_user",
})


# Monitor / TaskCreate / subagent events arrive as XML system-reminders injected
# into the user-message stream by the CLI:
#   <task-notification>
#     <task-id>...</task-id>
#     <tool-use-id>...</tool-use-id>
#     <status>completed</status>
#     <summary>...</summary>
#     <event>tick 1 at 00:39:07</event>
#   </task-notification>
# We extract these into structured payloads so the UI can render a compact
# block instead of leaking raw XML as a blue user bubble.
_TASK_XML_RE = re.compile(
    r"<(task-notification|task-started|task-progress)>(.*?)</\1>",
    re.DOTALL,
)
_TASK_FIELD_RE = re.compile(r"<([a-z\-]+)>(.*?)</\1>", re.DOTALL)


def parse_task_events(text: str) -> list[dict[str, Any]]:
    """Parse all task-* XML blocks out of a text body.

    Returns a list of structured dicts ready for the `task_event` WS payload.
    Empty list if no task blocks are present.
    """
    out: list[dict[str, Any]] = []
    for tag, inner in _TASK_XML_RE.findall(text):
        event_type = tag[len("task-"):]  # "notification" | "started" | "progress"
        fields: dict[str, str] = {}
        for k, v in _TASK_FIELD_RE.findall(inner):
            fields[k.replace("-", "_")] = v.strip()
        out.append({
            "event_type": event_type,
            "task_id": fields.get("task_id"),
            "tool_use_id": fields.get("tool_use_id"),
            "status": fields.get("status"),
            "summary": fields.get("summary"),
            # <event> is the per-stdout-line payload for Monitor events; treat
            # it like a description so the UI shows the actual log line.
            "description": fields.get("description") or fields.get("event"),
            "output_file": fields.get("output_file"),
        })
    return out


def strip_task_events(text: str) -> str:
    """Remove task-* XML blocks from a text body, collapse blank runs."""
    cleaned = _TASK_XML_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


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
        model: str | None = None,
    ) -> None:
        self._send = send
        self._cwd = cwd
        self._session_id = resume_session_id or session_id or str(uuid.uuid4())
        self._resume = resume_session_id
        self._model = model
        self._client: ClaudeSDKClient | None = None
        # Long-lived consumer of client.receive_messages(). We need to keep
        # reading between turns so Monitor / TaskCreate task-notifications that
        # the CLI emits AFTER a ResultMessage still surface to the UI in
        # real-time. receive_response() returns at ResultMessage and would
        # leave us blind to between-turn events.
        self._stream_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._permissions = PermissionBroker()
        self._permissions.set_auto_approve(auto_approve)
        # Per-prompt: forward fine-grained StreamEvent deltas (True) or hold
        # back and emit whole text/thinking blocks from the final
        # AssistantMessage (False). SDK option stays partial either way.
        self._stream_enabled = True
        # True between query() and the matching ResultMessage. Guards against
        # interleaved prompts on the same SDK session.
        self._busy = False
        # Set by the stream task when a ResultMessage arrives; cleared by
        # send_prompt before issuing the next query. Tests await this to know
        # the turn is done now that _stream_task is long-lived.
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        # Background tasks tailing per-task stdout files (Monitor / TaskCreate).
        # SDK only emits 3 typed events per task (started / updated / final
        # notification) — the actual per-line stdout sits in a file we have to
        # follow ourselves to get live ticks into the UI. Keyed by task_id.
        self._task_tails: dict[str, asyncio.Task[None]] = {}
        # True while the session is parked in the registry (WS disconnected).
        # Set by rebind(parked=True), cleared by rebind(parked=False).
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
        if self._client is not None:
            return

        # We always use 'default' permission mode and rely on can_use_tool to
        # gate or auto-approve. This gives us per-prompt override capability.
        # include_partial_messages=True turns on real token-by-token streaming:
        # the SDK emits StreamEvent objects with content_block_delta events
        # we forward as fine-grained text_delta / thinking events.
        options = ClaudeAgentOptions(
            cwd=self._cwd,
            setting_sources=["user", "project"],
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
            resume=self._resume,
            model=self._model,
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

        # Kick off the long-lived consumer immediately so between-turn task
        # events (Monitor / subagents) are captured even before the first
        # prompt is sent.
        self._stream_task = asyncio.create_task(self._consume_stream())

    async def stop(self) -> None:
        # Cancel all live tail tasks first so they don't try to send into a
        # disconnected WS after the SDK shuts down.
        for tail in list(self._task_tails.values()):
            tail.cancel()
        for tail in list(self._task_tails.values()):
            try:
                await tail
            except (asyncio.CancelledError, Exception):
                pass
        self._task_tails.clear()

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

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        """Send a prompt with optional image attachments.

        `images`: list of {"media_type": "image/png|jpeg|...", "data_b64": "..."}
        `stream`: when True, forward fine-grained text/thinking deltas as they
        arrive (token-by-token feel). When False (default), suppress deltas and
        emit complete text/thinking blocks from the final AssistantMessage —
        chunks per turn, easier to read.
        """
        if self._client is None:
            await self._send({
                "type": "error",
                "payload": {"code": "no_session", "message": "Session not started"},
            })
            return

        async with self._lock:
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

            if images:
                await self._client.query(
                    _build_multimodal_stream(text, images, self._session_id),
                    session_id=self._session_id,
                )
            else:
                await self._client.query(text, session_id=self._session_id)

    async def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception as e:
            logger.warning("sdk_interrupt_error", error=str(e))

    def set_auto_approve(self, value: bool) -> None:
        self._permissions.set_auto_approve(value)

    async def set_model(self, model: str | None) -> None:
        """Change the model mid-session via the SDK's set_model method."""
        if self._client is None:
            return
        try:
            await self._client.set_model(model or "")
            self._model = model
            logger.info("model_changed", model=model, session_id=self._session_id)
        except Exception as e:
            logger.warning("set_model_failed", error=str(e))
            await self._send({
                "type": "error",
                "payload": {"code": "set_model_failed", "message": str(e)},
            })

    def rebind(self, send: SendFn, *, parked: bool = False) -> None:
        """Re-attach a new WS send function after client reconnect / park.

        The long-lived ``_consume_stream`` task and any active ``_task_tails``
        capture ``self._send`` via attribute lookup, so replacing the attribute
        is enough — no restart needed.

        Pass ``parked=True`` when parking (WS disconnected) so the stream task
        knows to fire a push notification on the next ResultMessage.
        """
        self._send = send
        self._is_parked = parked

    async def notify_reconnected(self) -> None:
        """Tell the newly reconnected WS client that this session is live.

        Also re-sends any AskUserQuestion prompts the agent is still blocked on
        so the user can answer them on the new connection.
        """
        await self._send({
            "type": "session_started",
            "payload": {
                "session_id": self._session_id,
                "cwd": self._cwd,
                "resumed": True,
                "auto_approve": self._permissions.is_auto_approve,
                # Tell the frontend whether the session is mid-turn so it can
                # restore the streaming state before the history payload arrives.
                "is_busy": self._busy,
            },
        })
        await self._permissions.resend_pending_user_inputs(self._send)

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK callback — invoked for every tool call when permission_mode='default'."""
        logger.info("can_use_tool", tool_name=tool_name, session_id=self._session_id)
        if tool_name in _USER_INPUT_TOOLS:
            # Surface a question UI to the user rather than Allow/Deny.
            # The user's answer is injected back into the tool input so the
            # CLI's AskUserQuestion handler can pick it up in headless mode.
            tool_use_id = ctx.tool_use_id or _fallback_id()

            # AskUserQuestion passes data as:
            #   {questions: [{question, options: [{label, description}], ...}]}
            #   — note this list can contain MULTIPLE distinct questions, each
            #     with its own options and expecting its own answer.
            # Other tools (ask_followup_question) use a flat structure:
            #   {question, options: [...]}
            raw_questions = tool_input.get("questions")
            if isinstance(raw_questions, list) and raw_questions:
                questions_list: list[dict[str, Any]] = [
                    q for q in raw_questions if isinstance(q, dict)
                ]
            else:
                questions_list = [{
                    "question": tool_input.get("question", ""),
                    "options": (
                        tool_input.get("options")
                        or tool_input.get("follow_up")
                        or tool_input.get("suggestions")
                        or []
                    ),
                }]

            # Normalize each question to {question: str, options: list[str]}.
            # Options can be plain strings or {label, description} objects.
            normalized_questions: list[dict[str, Any]] = []
            for q in questions_list:
                raw_options = q.get("options") or []
                options: list[str] = []
                for opt in raw_options if isinstance(raw_options, list) else []:
                    if isinstance(opt, dict):
                        options.append(opt.get("label") or opt.get("value") or str(opt))
                    else:
                        options.append(str(opt))
                normalized_questions.append({
                    "question": q.get("question") or q.get("header") or "",
                    "options": options,
                })

            answers_list = await self._permissions.request_user_input(
                self._send,
                self._session_id,
                tool_use_id,
                normalized_questions,
            )
            logger.info(
                "user_input_answered",
                session_id=self._session_id,
                tool=tool_name,
                tool_input=tool_input,
                answers=answers_list,
            )
            # AskUserQuestion.answers is keyed by the full question text
            # (same convention as the annotations field per the tool schema).
            answers: dict[str, str] = {}
            for q, ans in zip(questions_list, answers_list):
                key = q.get("question") or q.get("header") or ""
                if key:
                    answers[key] = ans
            updated: dict[str, Any] = {
                **tool_input,
                # Back-compat scalar `answer` for tools that take a single
                # question (ask_followup_question / ask_user). For multi-question
                # AskUserQuestion the per-question `answers` dict is authoritative.
                "answer": answers_list[0] if answers_list else "",
            }
            if answers:
                updated["answers"] = answers
            return PermissionResultAllow(updated_input=updated)

        return await self._permissions.request(
            self._send, self._session_id, tool_name, tool_input, ctx
        )

    async def _consume_stream(self) -> None:
        """Long-lived consumer — runs for the lifetime of the session.

        Uses receive_messages() (not receive_response()) because the latter
        terminates at ResultMessage, leaving us deaf to task-notifications
        that Monitor / TaskCreate inject between turns.
        """
        assert self._client is not None
        try:
            async for msg in self._client.receive_messages():
                await self._dispatch(msg)
                if isinstance(msg, ResultMessage):
                    # Turn finished — one-shot auto-approve expires; we go idle.
                    self._permissions.disarm_one_shot()
                    self._busy = False
                    self._idle_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("sdk_stream_error", error=str(e), exc_info=True)
            await self._send({
                "type": "error",
                "payload": {"code": "sdk_stream_error", "message": str(e)},
            })
            self._busy = False
            self._idle_event.set()
            self._permissions.disarm_one_shot()

    async def _dispatch(self, msg: Any) -> None:
        # With include_partial_messages=True the SDK always emits fine-grained
        # StreamEvent objects. We decide per-prompt whether to forward those
        # (stream=True) or wait for the final AssistantMessage and forward whole
        # text/thinking blocks (stream=False).
        if isinstance(msg, StreamEvent):
            if self._stream_enabled:
                await self._dispatch_stream_event(msg)
            return

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    await self._send({
                        "type": "tool_call",
                        "payload": {
                            "session_id": self._session_id,
                            "tool_use_id": block.id,
                            "name": block.name,
                            "input": block.input,
                        },
                    })
                elif not self._stream_enabled and isinstance(block, TextBlock):
                    # Streaming OFF: emit the whole text block now (deltas were
                    # suppressed). ON: already streamed by _dispatch_stream_event.
                    if block.text:
                        await self._send({
                            "type": "text_delta",
                            "payload": {
                                "session_id": self._session_id,
                                "text": block.text,
                            },
                        })
                elif not self._stream_enabled and isinstance(block, ThinkingBlock):
                    if block.thinking:
                        await self._send({
                            "type": "thinking",
                            "payload": {
                                "session_id": self._session_id,
                                "text": block.thinking,
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
                elif isinstance(block, TextBlock):
                    # CLI injects task-* XML as system-reminders in user-message
                    # text (also visible in the .jsonl). We forward those as
                    # structured task_event so the UI can render them as
                    # compact blocks instead of leaking raw XML as a blue
                    # user bubble. Non-task user TextBlocks are dropped — the
                    # frontend already echoes the user's own prompt locally
                    # and pure-text system-reminders carry no useful UX info.
                    for ev in parse_task_events(block.text):
                        await self._send({
                            "type": "task_event",
                            "payload": {
                                "session_id": self._session_id,
                                "event_type": ev.get("event_type"),
                                "task_id": ev.get("task_id"),
                                "summary": ev.get("summary"),
                                "description": ev.get("description"),
                                "status": ev.get("status"),
                                "tool_use_id": ev.get("tool_use_id"),
                                "last_tool_name": None,
                            },
                        })

        elif isinstance(msg, SystemMessage):
            # Task lifecycle events from Monitor / TaskCreate / subagents stream
            # in between turns. SDK emits 3 kinds: task_started (kicked off),
            # task_updated (status change, e.g. completed), task_notification
            # (final summary with output_file). Per-stdout-line ticks are NOT
            # streamed by the SDK — they live in `output_file` only, so we
            # start a live tail on task_started to surface them as they land.
            if msg.subtype in (
                "task_started",
                "task_progress",
                "task_updated",
                "task_notification",
            ):
                data = msg.data or {}
                # `task_updated` deltas come under `patch` (e.g.
                # {"status": "completed", "end_time": ...}). Flatten so the
                # task_id / status logic below has a consistent shape.
                patch = data.get("patch") or {}
                status = data.get("status") or patch.get("status")
                task_id = data.get("task_id")

                # task_updated is redundant with the task_notification that
                # follows it (same status, same task_id) and pointless for the
                # UI on its own — render-wise it would show up as a duplicate
                # "task started · completed" block with no summary. Skip the
                # WS emit but still use it below to stop tailing.
                if msg.subtype != "task_updated":
                    event_type = msg.subtype.removeprefix("task_")  # started|progress|notification
                    await self._send({
                        "type": "task_event",
                        "payload": {
                            "session_id": self._session_id,
                            "event_type": event_type,
                            "task_id": task_id,
                            "summary": data.get("summary"),
                            "description": data.get("description"),
                            "status": status,
                            "tool_use_id": data.get("tool_use_id"),
                            "last_tool_name": data.get("last_tool_name"),
                        },
                    })

                # Start tailing on task_started so live stdout lines surface
                # while Monitor is still running instead of arriving in one
                # burst at the end.
                if msg.subtype == "task_started" and task_id:
                    self._start_task_tail(task_id, data.get("tool_use_id"))
                # Terminal status — give the tail a moment to drain any final
                # bytes, then cancel it. Same handling for both task_updated
                # (status=completed/failed/stopped in patch) and the final
                # task_notification.
                if status in ("completed", "failed", "stopped") and task_id:
                    self._stop_task_tail(task_id)
            else:
                await self._send({
                    "type": "system",
                    "payload": {
                        "session_id": self._session_id,
                        "subtype": msg.subtype,
                        "data": msg.data,
                    },
                })

        elif isinstance(msg, ResultMessage):
            # Always emit push_notify on result, via HTTP directly to the hub.
            # We deliberately do NOT use the WS callback (self._send) here:
            # the most important case for push is precisely when the client
            # WS is gone (PWA swiped away on Android), at which point the
            # WS callback is a no-op because the hub already closed the
            # hub↔worker connection and the WS manager has dropped the
            # connection_id. HTTP is independent of any client lifecycle,
            # so it works whether the session is parked or actively
            # connected. Same dedup story applies (SW + local notify share
            # tag "claude-result").
            await self._emit_push_notify(
                title="Claude finished",
                body=f"Task completed in {self._cwd or 'background'}",
            )
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

    def _task_output_path(self, task_id: str) -> Path | None:
        """Compute the canonical path the CLI uses to mirror task stdout.

        Format observed: /tmp/claude-{uid}/{cwd-slug}/{session_id}/tasks/{task_id}.output
        where cwd-slug is the cwd with every non-alphanumeric character (in
        particular '/' AND '_') replaced by '-'. So
        /home/kgawrys/code/claude_cloud → -home-kgawrys-code-claude-cloud.
        Returns None if we don't have enough context (no cwd) to build the path.
        """
        if not self._cwd:
            return None
        cwd_slug = re.sub(r"[^A-Za-z0-9]", "-", self._cwd)
        return Path(
            f"/tmp/claude-{os.getuid()}/{cwd_slug}/"
            f"{self._session_id}/tasks/{task_id}.output"
        )

    def _start_task_tail(self, task_id: str, tool_use_id: str | None) -> None:
        """Spawn a background task that streams new stdout lines as task_event."""
        if task_id in self._task_tails:
            return
        self._task_tails[task_id] = asyncio.create_task(
            self._tail_task_output(task_id, tool_use_id)
        )

    def _stop_task_tail(self, task_id: str) -> None:
        """Give the tail half a second to flush, then cancel it.

        Called on terminal task status (completed/failed/stopped). The short
        grace period lets the final stdout line(s) reach us before we stop
        polling, since the CLI may write the last bytes after emitting the
        terminal status event.
        """
        tail = self._task_tails.get(task_id)
        if tail is None or tail.done():
            self._task_tails.pop(task_id, None)
            return

        async def _drain_and_cancel() -> None:
            await asyncio.sleep(0.5)
            tail.cancel()
            try:
                await tail
            except (asyncio.CancelledError, Exception):
                pass
            self._task_tails.pop(task_id, None)

        asyncio.create_task(_drain_and_cancel())

    async def _tail_task_output(
        self, task_id: str, tool_use_id: str | None
    ) -> None:
        """Poll the per-task stdout file and emit each new line as a task_event.

        The file is created lazily by the CLI when the task first writes. We
        retry-open for a few seconds, then bail if it never appears (e.g. on
        a platform where the path convention differs).
        """
        path = self._task_output_path(task_id)
        if path is None:
            return

        # Wait for the file (up to ~10s).
        for _ in range(40):
            if path.exists():
                break
            await asyncio.sleep(0.25)
        else:
            logger.warning(
                "task_output_missing", task_id=task_id, path=str(path)
            )
            return

        try:
            # Polling tail — readline returns "" at EOF, sleep and try again.
            with path.open("r") as f:
                buffer = ""
                while True:
                    chunk = f.readline()
                    if not chunk:
                        if buffer:
                            await self._emit_task_line(task_id, tool_use_id, buffer)
                            buffer = ""
                        await asyncio.sleep(0.2)
                        continue
                    if chunk.endswith("\n"):
                        line = (buffer + chunk).rstrip("\n")
                        buffer = ""
                        if line:
                            await self._emit_task_line(task_id, tool_use_id, line)
                    else:
                        buffer += chunk
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("task_tail_error", task_id=task_id, error=str(e))

    async def _emit_task_line(
        self, task_id: str, tool_use_id: str | None, line: str
    ) -> None:
        await self._send({
            "type": "task_event",
            "payload": {
                "session_id": self._session_id,
                "event_type": "progress",
                "task_id": task_id,
                "summary": None,
                "description": line,
                "status": None,
                "tool_use_id": tool_use_id,
                "last_tool_name": None,
            },
        })

    async def _dispatch_stream_event(self, msg: StreamEvent) -> None:
        """Forward fine-grained Anthropic stream events as small WS deltas.

        Only content_block_delta carries useful incremental text/thinking;
        other events (message_start/stop, content_block_start/stop, message_delta)
        are SDK plumbing we can ignore — the assembled AssistantMessage handles
        tool_use and the ResultMessage handles completion.
        """
        ev = msg.event
        if ev.get("type") != "content_block_delta":
            return
        delta = ev.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text", "")
            if text:
                await self._send({
                    "type": "text_delta",
                    "payload": {"session_id": self._session_id, "text": text},
                })
        elif dtype == "thinking_delta":
            text = delta.get("thinking", "")
            if text:
                await self._send({
                    "type": "thinking",
                    "payload": {"session_id": self._session_id, "text": text},
                })
        # input_json_delta (partial tool input) is ignored — the final
        # AssistantMessage carries the fully assembled tool input.

    async def _emit_push_notify(self, *, title: str, body: str) -> None:
        """Deliver a push notification to the hub out-of-band over HTTP.

        Independent of the client WS — works whether the session is parked
        or actively connected. Best-effort: swallow all errors and just log
        them, since failing to push must never disrupt the agent loop.
        """
        hub_url = os.environ.get("HUB_URL", "").rstrip("/")
        secret = os.environ.get("WORKER_SECRET", "")
        if not hub_url or not secret:
            logger.debug("push_notify_skipped_no_hub_config")
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


async def _build_multimodal_stream(
    text: str,
    images: list[dict[str, str]],
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """Build the stream-input message: one user message with text + image blocks.

    `images` items: {"media_type": "image/png|jpeg|gif|webp", "data_b64": "..."}.
    Anything else is ignored.
    """
    allowed = {"image/png", "image/jpeg", "image/gif", "image/webp"}

    content_blocks: list[dict[str, Any]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    for img in images:
        media_type = img.get("media_type", "")
        data_b64 = img.get("data_b64", "")
        if media_type not in allowed or not data_b64:
            continue
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data_b64,
            },
        })

    # If the user provided only invalid images and no text, still ship the text.
    if not content_blocks:
        content_blocks.append({"type": "text", "text": text or ""})

    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }
