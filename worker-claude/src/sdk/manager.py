"""SessionManager — owns the active Session per WS connection.

Coordinates with LockManager to enforce one-writer-per-session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from src.locks.manager import LockManager, locks
from src.sdk.registry import registry
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
        if not Path(cwd).is_dir():
            await self._send({
                "type": "error",
                "payload": {
                    "code": "cwd_not_found",
                    "message": f"Project directory not available: {cwd}",
                },
            })
            return None

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
        """Resume an existing session, acquiring its lock (force=takeover).

        Checks the in-process :mod:`registry` first — if the session was parked
        after a WS disconnect it is reclaimed (fast path, no SDK restart needed).
        Falls back to a full SDK resume from the JSONL transcript on disk.
        """
        if not Path(cwd).is_dir():
            await self._send({
                "type": "error",
                "payload": {
                    "code": "cwd_not_found",
                    "message": f"Project directory not available: {cwd}",
                },
            })
            return None

        await self._release_current_lock()

        # ── Fast path: session is parked in the registry ─────────────────────
        parked = registry.take(session_id)
        if parked is not None:
            acquired, existing = await self._locks.try_acquire(
                session_id=session_id,
                connection_id=self._connection_id,
                device_label=self._device_label,
                notify=self._on_lock_revoked_factory(session_id),
                force=force,
            )
            if not acquired and existing is not None:
                # Another client holds the lock — put the session back and
                # surface the conflict to the caller.
                registry.park(parked)
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
            parked.rebind(self._send)
            parked.set_auto_approve(auto_approve)
            self._current = parked
            await parked.notify_reconnected()
            logger.info(
                "session_reattached",
                session_id=session_id,
                connection_id=self._connection_id,
            )
            return parked

        # ── Slow path: full SDK resume from disk ──────────────────────────────
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

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> bool:
        if self._current is None:
            await self._send({
                "type": "error",
                "payload": {
                    "code": "no_session",
                    "message": "Start or resume a session before sending prompts",
                },
            })
            return False
        await self._current.send_prompt(
            text,
            auto_approve_once=auto_approve_once,
            images=images,
            stream=stream,
        )
        return True

    def resolve_user_input(self, tool_use_id: str, answers: list[str]) -> bool:
        """Deliver user answers to a pending AskUserQuestion tool call.

        AskUserQuestion can pose multiple questions in one call — ``answers``
        is one string per question, in the same order they were sent.
        """
        if self._current is None:
            return False
        return self._current.permissions.resolve_user_input(tool_use_id, answers)

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
        """Called on WS disconnect: park the active session in the registry.

        The session keeps running so an in-progress agent turn is not
        interrupted. If no client reclaims it within the registry TTL it will
        be stopped automatically.
        """
        if self._current is not None:
            registry.park(self._current)
            self._current = None
        # Release the WS-level lock so a reconnecting client can re-acquire it
        # without needing force=True.
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
