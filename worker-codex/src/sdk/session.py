"""Wrapper around openai-codex-sdk's Codex + Thread classes.

Implements SessionProtocol (worker_shared.sdk.base): one Session per
active conversation, bound to a project cwd, owns the SDK thread
lifecycle, translates Codex's event stream into the WS messages the
frontend already understands (the same shape as worker-claude and
worker-copilot).

Codex SDK event model (per openai-codex-sdk):
    streamed = await thread.run_streamed(prompt)
    async for event in streamed.events:
        if event.type == "item.completed":
            # event.item carries an assistant_message, tool_call,
            # command_execution, reasoning, or similar artifact -
            # the SDK does not document the full schema, so we
            # introspect each item defensively.
        elif event.type == "turn.completed":
            # event.usage carries token counts.

Image input: Codex expects {type: "local_image", path: "..."} so we
spill base64 attachments to a per-session temp dir and pass the file
path. The temp dir is cleaned up in stop().

Known limitations vs worker-claude / worker-copilot (see also
sdk/permissions.py):
- No programmatic permission callback; Codex CLI's approval policy is
  configured upstream in ~/.codex/config.json. The frontend's Allow /
  Deny UI will not light up for Codex tool calls.
- Token-by-token text streaming is not exposed by the SDK; we emit
  whole text blocks at item.completed. The frontend `stream` flag is
  accepted but has no observable effect.
- No native ask-the-user-a-question tool, so user_input_request never
  fires from Codex.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from worker_shared.sdk.push_notify import emit_push_notify

from src.sdk.client import get_client, sdk_available, sdk_import_error
from src.sdk.permissions import PermissionBroker

logger = structlog.get_logger()


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class Session:
    """One Codex SDK thread bound to a project cwd + optional resume id."""

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
        self._thread: object | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._permissions = PermissionBroker()
        self._permissions.set_auto_approve(auto_approve)
        self._stream_enabled = True
        self._busy = False
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._turn_started_at: float | None = None
        self._is_parked = False
        # Temp dir for spilled base64 image attachments. Created on
        # first prompt that has images; cleaned up in stop().
        self._image_tempdir: Path | None = None

    # ── SessionProtocol surface ─────────────────────────────────────

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
        if self._thread is not None:
            return
        if not sdk_available():
            await self._send({
                "type": "error",
                "payload": {
                    "code": "sdk_unavailable",
                    "message": (
                        "openai-codex-sdk is not installed in this worker: "
                        f"{sdk_import_error()}"
                    ),
                },
            })
            return

        client = await get_client()
        try:
            if self._resume:
                # resume_thread expects the Codex-side thread_id; if our
                # placeholder UUID isn't a real one this raises and we
                # surface the error to the WS.
                self._thread = client.resume_thread(self._resume)  # type: ignore[attr-defined]
            else:
                options: dict[str, Any] = {
                    "working_directory": self._cwd,
                    "skip_git_repo_check": True,
                }
                if self._model:
                    options["model"] = self._model
                self._thread = client.start_thread(options)  # type: ignore[attr-defined]
        except Exception as e:
            logger.error("codex_session_start_failed", error=str(e), exc_info=True)
            await self._send({
                "type": "error",
                "payload": {"code": "session_start_failed", "message": str(e)},
            })
            return

        # Sync session_id to whatever the SDK actually allocated for the
        # thread so the frontend sees a consistent id.
        actual_id = getattr(self._thread, "id", None) or getattr(
            self._thread, "thread_id", None
        )
        if isinstance(actual_id, str) and actual_id:
            self._session_id = actual_id

        logger.info(
            "codex_session_started",
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
        """Disconnect the SDK thread and clean up per-session temp state."""
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._consumer_task = None

        self._permissions.cancel_all("Session stopped")

        # openai-codex-sdk currently has no explicit thread.close() in
        # the published API; drop the reference and let the underlying
        # subprocess cleanup happen on Codex client shutdown.
        self._thread = None

        if self._image_tempdir is not None:
            try:
                shutil.rmtree(self._image_tempdir, ignore_errors=True)
            except Exception as e:
                logger.warning("codex_image_tempdir_cleanup_failed", error=str(e))
            self._image_tempdir = None

        logger.info("codex_session_stopped", session_id=self._session_id)

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        if self._thread is None:
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
        # Per-prompt stream flag is stored for symmetry with the other
        # workers but the SDK doesn't expose token deltas - see module
        # docstring.
        self._stream_enabled = stream
        self._busy = True
        self._idle_event.clear()
        self._turn_started_at = time.monotonic()

        prompt_payload: str | list[dict[str, str]] = text
        if images:
            allowed = {"image/png", "image/jpeg", "image/gif", "image/webp"}
            blocks: list[dict[str, str]] = [{"type": "text", "text": text}] if text else []
            for img in images:
                mt = img.get("media_type", "")
                data_b64 = img.get("data_b64", "")
                if mt not in allowed or not data_b64:
                    continue
                ext = mt.split("/", 1)[1] if "/" in mt else "bin"
                path = self._spill_image(data_b64, ext)
                if path is not None:
                    blocks.append({"type": "local_image", "path": str(path)})
            if blocks:
                prompt_payload = blocks

        try:
            streamed = await self._thread.run_streamed(prompt_payload)  # type: ignore[attr-defined]
        except Exception as e:
            logger.error("codex_send_failed", error=str(e), exc_info=True)
            self._busy = False
            self._idle_event.set()
            self._permissions.disarm_one_shot()
            await self._send({
                "type": "error",
                "payload": {"code": "send_failed", "message": str(e)},
            })
            return

        # Consume the event stream in the background so send_prompt
        # returns immediately to the WS handler (so subsequent ping /
        # interrupt messages aren't blocked behind the turn).
        self._consumer_task = asyncio.create_task(self._consume(streamed))

    async def interrupt(self) -> None:
        if self._thread is None:
            return
        # openai-codex-sdk doesn't currently expose a documented
        # cancel() on Thread; best effort - cancel our local consumer
        # so we stop forwarding events, and let the next prompt's
        # implicit busy=False reset things.
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._consumer_task = None
        self._busy = False
        self._idle_event.set()

    def set_auto_approve(self, value: bool) -> None:
        self._permissions.set_auto_approve(value)

    async def set_model(self, model: str | None) -> None:
        # No documented mid-thread set_model in openai-codex-sdk. Record
        # the preference; the next start_thread() (e.g. after /new) will
        # honor it.
        self._model = model
        await self._send({
            "type": "system",
            "payload": {
                "session_id": self._session_id,
                "subtype": "model_changed",
                "data": {"model": model},
            },
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

    # ── Internals ───────────────────────────────────────────────────

    def _spill_image(self, data_b64: str, ext: str) -> Path | None:
        """Write a base64 image blob to disk and return the path.

        Codex SDK wants `local_image` with a file path, not an inline
        base64 blob like Claude / Copilot. We dump to a per-session
        tempdir cleaned up in stop().
        """
        if self._image_tempdir is None:
            self._image_tempdir = Path(
                tempfile.mkdtemp(prefix=f"codex-img-{self._session_id}-")
            )
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except Exception as e:
            logger.warning("codex_image_decode_failed", error=str(e))
            return None
        path = self._image_tempdir / f"{uuid.uuid4().hex}.{ext}"
        try:
            path.write_bytes(raw)
        except OSError as e:
            logger.warning("codex_image_spill_failed", error=str(e))
            return None
        return path

    async def _consume(self, streamed: object) -> None:
        """Drain Codex's event stream, translate to WS messages."""
        try:
            async for event in streamed.events:  # type: ignore[attr-defined]
                await self._dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("codex_stream_error", error=str(e), exc_info=True)
            await self._send({
                "type": "error",
                "payload": {"code": "stream_error", "message": str(e)},
            })
        finally:
            await self._finish_turn()

    async def _dispatch(self, event: object) -> None:
        """Map one Codex SDK event onto the WS message protocol.

        The SDK does not publish a stable typed schema for items; we
        introspect each one by attribute presence and skip anything
        we don't recognize. Unknown items show up at debug log level
        with their attribute names so iterating on the mapping is a
        matter of running once and reading the logs.
        """
        etype = getattr(event, "type", None)

        if etype == "turn.completed":
            await self._finish_turn(event=event)
            return

        if etype != "item.completed":
            logger.debug("codex_unknown_event", etype=etype)
            return

        item = getattr(event, "item", None)
        if item is None:
            return

        item_type = (
            getattr(item, "type", None)
            or getattr(item, "kind", None)
            or ""
        )

        # ── Text / assistant message ────────────────────────────────
        text = (
            getattr(item, "text", None)
            or getattr(item, "content", None)
            or getattr(item, "final_response", None)
        )
        if item_type in {"assistant_message", "message", "response"} and isinstance(text, str) and text:
            await self._send({
                "type": "text_delta",
                "payload": {"session_id": self._session_id, "text": text},
            })
            return

        # ── Reasoning / thinking ────────────────────────────────────
        reasoning = (
            getattr(item, "reasoning", None)
            or getattr(item, "thinking", None)
        )
        if item_type in {"reasoning", "thinking"} and isinstance(reasoning, str) and reasoning:
            await self._send({
                "type": "thinking",
                "payload": {"session_id": self._session_id, "text": reasoning},
            })
            return

        # ── Tool / command / file change ────────────────────────────
        tool_name = (
            getattr(item, "tool_name", None)
            or getattr(item, "name", None)
            or getattr(item, "command", None)
        )
        tool_args = (
            getattr(item, "arguments", None)
            or getattr(item, "args", None)
            or getattr(item, "input", None)
        )
        tool_id = (
            getattr(item, "id", None)
            or getattr(item, "call_id", None)
            or f"codex_{uuid.uuid4().hex[:8]}"
        )
        tool_result = (
            getattr(item, "result", None)
            or getattr(item, "output", None)
        )
        tool_error = getattr(item, "error", None)

        if tool_name:
            # Codex collapses tool start + completion into a single
            # item.completed; emit both messages back-to-back so the
            # frontend's "Running tool / Done" UX still works.
            await self._send({
                "type": "tool_call",
                "payload": {
                    "session_id": self._session_id,
                    "tool_use_id": tool_id,
                    "name": str(tool_name),
                    "input": tool_args if isinstance(tool_args, dict | list) else {"raw": tool_args},
                },
            })
            content: Any = tool_result if tool_result is not None else tool_error
            if content is not None and not isinstance(content, str | int | float | bool | list | dict):
                content = str(content)
            await self._send({
                "type": "tool_result",
                "payload": {
                    "session_id": self._session_id,
                    "tool_use_id": tool_id,
                    "content": content,
                    "is_error": bool(tool_error),
                },
            })
            return

        # ── Fallback: dump as system event for visibility ───────────
        attrs = [a for a in dir(item) if not a.startswith("_")]
        logger.debug(
            "codex_unmapped_item",
            item_type=item_type,
            attrs=attrs[:20],
        )
        await self._send({
            "type": "system",
            "payload": {
                "session_id": self._session_id,
                "subtype": "codex_item",
                "data": {"item_type": str(item_type), "attrs": attrs[:20]},
            },
        })

    async def _finish_turn(self, *, event: object | None = None) -> None:
        """Emit the final `result` for the turn (idempotent)."""
        if not self._busy:
            return
        duration_ms = 0
        if self._turn_started_at is not None:
            duration_ms = int((time.monotonic() - self._turn_started_at) * 1000)
            self._turn_started_at = None
        usage = getattr(event, "usage", None) if event is not None else None
        total_tokens = 0
        if usage is not None:
            total_tokens = (
                getattr(usage, "total_tokens", 0)
                or getattr(usage, "total", 0)
                or 0
            )
        self._permissions.disarm_one_shot()
        self._busy = False
        self._idle_event.set()

        await emit_push_notify(
            title="Codex finished",
            body=f"Task completed in {self._cwd or 'background'}",
        )
        await self._send({
            "type": "result",
            "payload": {
                "session_id": self._session_id,
                "subtype": "success",
                "duration_ms": duration_ms,
                # openai-codex-sdk does not surface a cost field directly;
                # frontend treats 0.0 as "unknown" so this is safe.
                "total_cost_usd": 0.0,
                "num_turns": 1,
                "is_error": False,
                "total_tokens": int(total_tokens) if total_tokens else None,
            },
        })
