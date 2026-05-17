"""SessionManager — owns the active Session per WS connection.

Coordinates with LockManager to enforce one-writer-per-session.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.locks.manager import LockManager, locks
from src.sdk.session import SendFn, Session

logger = structlog.get_logger()


class SessionManager:
    """Owns the active Session for a WS connection."""

    def __init__(
        self,
        send: SendFn,
        *,
        connection_id: str,
        device_label: str = "unknown",
        lock_manager: LockManager | None = None,
    ) -> None:
        self._send = send
        self._connection_id = connection_id
        self._device_label = device_label
        self._locks = lock_manager or locks
        self._current: Session | None = None

    @property
    def current(self) -> Session | None:
        return self._current

    async def new_session(self, cwd: str, *, auto_approve: bool = False) -> Session | None:
        """Start a fresh session bound to `cwd`. Returns the session (or None on lock fail)."""
        await self._release_current_lock()
        session = Session(send=self._send, cwd=cwd, auto_approve=auto_approve)

        acquired, _ = await self._locks.try_acquire(
            session_id=session.session_id,
            connection_id=self._connection_id,
            device_label=self._device_label,
            notify=self._on_lock_revoked_factory(session.session_id),
        )
        if not acquired:
            # Should never happen for new session (UUID is fresh)
            await self._send({
                "type": "error",
                "payload": {"code": "lock_collision", "message": "Unexpected lock collision"},
            })
            return None

        await self._stop_current()
        await session.start()
        self._current = session
        return session

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        *,
        force: bool = False,
        auto_approve: bool = False,
    ) -> Session | None:
        """Resume an existing session, acquiring its lock (force=takeover)."""
        await self._release_current_lock()

        acquired, existing = await self._locks.try_acquire(
            session_id=session_id,
            connection_id=self._connection_id,
            device_label=self._device_label,
            notify=self._on_lock_revoked_factory(session_id),
            force=force,
        )
        if not acquired and existing is not None:
            await self._send({
                "type": "session_locked",
                "payload": {
                    "session_id": session_id,
                    "locked_by": existing.device_label,
                    "locked_at": existing.locked_at,
                },
            })
            return None

        await self._stop_current()
        session = Session(
            send=self._send,
            cwd=cwd,
            resume_session_id=session_id,
            auto_approve=auto_approve,
        )
        await session.start()
        self._current = session
        return session

    async def send_prompt(self, text: str, *, auto_approve_once: bool = False) -> bool:
        if self._current is None:
            await self._send({
                "type": "error",
                "payload": {
                    "code": "no_session",
                    "message": "Start or resume a session before sending prompts",
                },
            })
            return False
        await self._current.send_prompt(text, auto_approve_once=auto_approve_once)
        return True

    def resolve_permission(
        self,
        tool_use_id: str,
        *,
        allow: bool,
        reason: str = "",
    ) -> bool:
        """Resolve a pending permission request on the active session."""
        if self._current is None:
            return False
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        result = (
            PermissionResultAllow()
            if allow
            else PermissionResultDeny(message=reason or "Denied by user")
        )
        return self._current.permissions.resolve(tool_use_id, result)

    def set_auto_approve(self, value: bool) -> None:
        if self._current is not None:
            self._current.set_auto_approve(value)

    async def interrupt(self) -> None:
        if self._current is not None:
            await self._current.interrupt()

    async def stop(self) -> None:
        """Tear down the active session and release all locks held by this connection."""
        await self._stop_current()
        await self._locks.release_all_by_connection(self._connection_id)

    async def _stop_current(self) -> None:
        if self._current is not None:
            try:
                await self._current.stop()
            except Exception as e:
                logger.warning("session_stop_error", error=str(e))
            self._current = None

    async def _release_current_lock(self) -> None:
        if self._current is not None:
            await self._locks.release(self._current.session_id, self._connection_id)

    def _on_lock_revoked_factory(self, session_id: str) -> Any:
        """Build a callback that runs when *this* connection's lock is taken away."""

        async def _on_revoked(msg: dict[str, Any]) -> None:
            # Send to the WS client so it can drop to read-only
            await self._send(msg)
            # Stop the active session if it's the one being revoked
            if self._current is not None and self._current.session_id == session_id:
                await self._stop_current()

        return _on_revoked
