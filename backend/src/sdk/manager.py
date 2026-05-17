"""SessionManager — owns the active Session per WS connection.

Phase 3: one active Session at a time per WS (switch via new/resume).
Multi-session-per-connection lands later if needed.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.sdk.session import SendFn, Session

logger = structlog.get_logger()


class SessionManager:
    """Owns the active Session for a WS connection."""

    def __init__(self, send: SendFn) -> None:
        self._send = send
        self._current: Session | None = None

    @property
    def current(self) -> Session | None:
        return self._current

    async def new_session(self, cwd: str) -> Session:
        """Start a fresh session bound to `cwd`."""
        await self._stop_current()
        session = Session(send=self._send, cwd=cwd)
        await session.start()
        self._current = session
        return session

    async def resume_session(self, cwd: str, session_id: str) -> Session:
        """Resume an existing session by id, bound to `cwd`."""
        await self._stop_current()
        session = Session(send=self._send, cwd=cwd, resume_session_id=session_id)
        await session.start()
        self._current = session
        return session

    async def send_prompt(self, text: str) -> bool:
        """Forward a prompt to the active session. Returns False if no session."""
        if self._current is None:
            await self._send({
                "type": "error",
                "payload": {
                    "code": "no_session",
                    "message": "Start or resume a session before sending prompts",
                },
            })
            return False
        await self._current.send_prompt(text)
        return True

    async def interrupt(self) -> None:
        if self._current is not None:
            await self._current.interrupt()

    async def stop(self) -> None:
        """Tear down the active session (called on WS disconnect)."""
        await self._stop_current()

    async def _stop_current(self) -> None:
        if self._current is not None:
            try:
                await self._current.stop()
            except Exception as e:
                logger.warning("session_stop_error", error=str(e))
            self._current = None


async def emit(send: SendFn, msg_type: str, payload: dict[str, Any]) -> None:
    """Helper to send a WS message."""
    await send({"type": msg_type, "payload": payload})
