"""Global session registry for detached (WS-disconnected) sessions.

When a WebSocket disconnects, the active Session is parked here instead of
being stopped. The SDK stream, tool tails, and any in-progress tool calls
continue running. When the same client reconnects it reclaims the session by
calling :func:`take` and rebinding its send function.

Sessions expire after :data:`DETACH_TTL` seconds without being reclaimed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from src.sdk.session import Session

logger = structlog.get_logger()

# How long a parked session survives without a client (seconds).
# Raise this if you regularly kick off long-running agent tasks and close
# the browser — the session keeps running in the background until reclaimed
# or until this TTL expires. Each parked session holds an SDK subprocess in
# memory, so don't set this arbitrarily high on memory-constrained machines.
DETACH_TTL = 60 * 60  # 1 hour


class SessionRegistry:
    """In-memory registry of WS-detached sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}

    def park(self, session: Session) -> None:
        """Park a session after its WS disconnects.

        The session keeps running; an expiry timer is started so it gets
        cleaned up if no client reclaims it within :data:`DETACH_TTL` seconds.

        Critically:
        - Replace ``session._send`` with a no-op so the running stream task
          doesn't crash when it tries to write to the now-closed WebSocket.
        - Cancel any pending permission / user-input futures so the SDK loop
          can continue executing tool calls without blocking forever.
        """
        sid = session.session_id
        # Cancel any prior expiry task (shouldn't happen, but be safe)
        existing_task = self._expiry_tasks.pop(sid, None)
        if existing_task:
            existing_task.cancel()

        # Swap in a silent no-op send so stream messages don't blow up.
        async def _noop_send(msg: Any) -> None:  # noqa: ARG001
            pass

        session.rebind(_noop_send, parked=True)

        # Cancel only allow/deny permission futures — the agent can't wait for
        # a human decision while detached. User-input (AskUserQuestion) futures
        # are intentionally kept alive: the agent stays blocked on them, and
        # after reconnect notify_reconnected() re-sends the question.
        session.permissions.cancel_permissions("Client disconnected — session running in background")

        self._sessions[sid] = session
        self._expiry_tasks[sid] = asyncio.create_task(self._expire(sid))
        logger.info("session_parked", session_id=sid, ttl_s=DETACH_TTL)

    def take(self, session_id: str) -> Session | None:
        """Claim a parked session. Cancels its expiry timer.

        Returns the session or None if not parked.
        """
        session = self._sessions.pop(session_id, None)
        task = self._expiry_tasks.pop(session_id, None)
        if task:
            task.cancel()
        if session:
            logger.info("session_reclaimed", session_id=session_id)
        return session

    def has(self, session_id: str) -> bool:
        return session_id in self._sessions

    @property
    def parked_count(self) -> int:
        return len(self._sessions)

    async def _expire(self, session_id: str) -> None:
        try:
            await asyncio.sleep(DETACH_TTL)
        except asyncio.CancelledError:
            return
        session = self._sessions.pop(session_id, None)
        self._expiry_tasks.pop(session_id, None)
        if session:
            logger.info("session_expired", session_id=session_id, reason="ttl")
            try:
                await session.stop()
            except Exception as exc:
                logger.warning("session_expire_stop_error", session_id=session_id, error=str(exc))

    async def stop_all(self) -> None:
        """Stop every parked session — called on server shutdown."""
        for task in list(self._expiry_tasks.values()):
            task.cancel()
        self._expiry_tasks.clear()
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            try:
                await session.stop()
            except Exception:
                pass
        logger.info("registry_stopped_all", count=len(sessions))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SessionRegistry parked={self.parked_count}>"

    # ---- dict-like helpers used by tests --------------------------------

    def __contains__(self, session_id: Any) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)


# Module-level singleton shared across the process.
registry = SessionRegistry()
