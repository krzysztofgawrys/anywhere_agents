"""SessionManager - owns the active Copilot Session per WS connection.

Mirror of worker-claude/sdk/manager.py: coordinates with LockManager to
enforce one-writer-per-session, parks the session in the shared registry
on WS disconnect so an in-progress agent turn isn't interrupted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from worker_shared.locks.manager import LockManager, locks
from worker_shared.sdk.registry import registry

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

    async def new_session(
        self, cwd: str, *, auto_approve: bool = False, model: str | None = None,
    ) -> Session | None:
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
        session = Session(send=self._send, cwd=cwd, auto_approve=auto_approve, model=model)

        acquired, _ = await self._locks.try_acquire(
            session_id=session.session_id,
            connection_id=self._connection_id,
            device_label=self._device_label,
            notify=self._on_lock_revoked_factory(session.session_id),
        )
        if not acquired:
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
        model: str | None = None,
    ) -> Session | None:
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

        # Fast path: reclaim from in-process registry
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
            # The parked object is a Session (our class). Tell mypy.
            self._current = parked  # type: ignore[assignment]
            await self._current.notify_reconnected()  # type: ignore[union-attr]
            logger.info(
                "session_reattached",
                session_id=session_id,
                connection_id=self._connection_id,
            )
            return self._current

        # Slow path: full SDK resume from disk via CopilotClient.resume_session
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
            model=model,
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
        if self._current is None:
            return False
        return self._current.permissions.resolve(tool_use_id, allow=allow)

    def set_auto_approve(self, value: bool) -> None:
        if self._current is not None:
            self._current.set_auto_approve(value)

    async def set_model(self, model: str | None) -> None:
        if self._current is not None:
            await self._current.set_model(model)

    async def interrupt(self) -> None:
        if self._current is not None:
            await self._current.interrupt()

    async def stop(self) -> None:
        """WS disconnect: park the active session so a turn isn't interrupted."""
        if self._current is not None:
            registry.park(self._current)  # type: ignore[arg-type]
            self._current = None
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
        async def _on_revoked(msg: dict[str, Any]) -> None:
            await self._send(msg)
            if self._current is not None and self._current.session_id == session_id:
                await self._stop_current()
        return _on_revoked
