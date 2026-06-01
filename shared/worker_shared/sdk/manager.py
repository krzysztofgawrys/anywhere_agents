"""Generic SessionManager - one active Session per WS connection.

Lifted from the (formerly duplicated) per-worker SessionManager classes.
The manager itself is agent-agnostic: it does lock coordination,
registry parking on WS disconnect, fast-path session reattach, and
forwards `send_prompt` / `set_model` / `interrupt` / `resolve_*` to
the active Session.

A worker plugs in by passing a `SessionFactory` (typically the worker's
own `Session` class) to the manager. The factory is invoked with
`(send=..., cwd=..., resume_session_id=..., auto_approve=..., model=...)`
so the Session signature must accept those keyword arguments.

Permission resolution uses a uniform `(allow, reason)` shape via
`PermissionsProtocol.resolve()`. Adapters whose SDK takes its own
permission result type (e.g. claude_agent_sdk.PermissionResultAllow /
PermissionResultDeny) build that inside their own `permissions.resolve`
implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from worker_shared.locks.manager import LockManager, locks
from worker_shared.sdk.base import SendFn, SessionFactory, SessionProtocol
from worker_shared.sdk.registry import registry

logger = structlog.get_logger()


class SessionManager:
    """Owns the active Session for one WS connection.

    Construct with a `session_factory` that builds your worker's
    concrete Session class. Everything below is reused as-is across
    workers.
    """

    def __init__(
        self,
        send: SendFn,
        *,
        session_factory: SessionFactory,
        connection_id: str,
        device_label: str = "unknown",
        lock_manager: LockManager | None = None,
    ) -> None:
        self._send = send
        self._factory = session_factory
        self._connection_id = connection_id
        self._device_label = device_label
        self._locks = lock_manager or locks
        self._current: SessionProtocol | None = None

    @property
    def current(self) -> SessionProtocol | None:
        return self._current

    # ── Public lifecycle ─────────────────────────────────────────────

    async def new_session(
        self,
        cwd: str,
        *,
        auto_approve: bool = False,
        model: str | None = None,
    ) -> SessionProtocol | None:
        """Start a fresh session bound to `cwd`. Returns it (or None on lock fail)."""
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
        session = self._factory(
            send=self._send,
            cwd=cwd,
            auto_approve=auto_approve,
            model=model,
        )

        acquired, _ = await self._locks.try_acquire(
            session_id=session.session_id,
            connection_id=self._connection_id,
            device_label=self._device_label,
            notify=self._on_lock_revoked_factory(session.session_id),
        )
        if not acquired:
            # Fresh UUID should never collide; fail loud if it does.
            await self._send({
                "type": "error",
                "payload": {
                    "code": "lock_collision",
                    "message": "Unexpected lock collision",
                },
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
    ) -> SessionProtocol | None:
        """Resume an existing session, acquiring its lock (force=takeover).

        Checks the in-process :mod:`registry` first - if the session
        was parked after a WS disconnect, it is reclaimed without
        restarting the SDK (fast path). Otherwise falls back to a
        full SDK resume from on-disk transcript.
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

        # ── Fast path: session is parked in the registry ────────────
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
                # Another client holds the lock; put the session back
                # and surface the conflict.
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
            # `parked` is a SessionProtocol from this worker's factory.
            parked.rebind(self._send)  # type: ignore[attr-defined]
            parked.set_auto_approve(auto_approve)  # type: ignore[attr-defined]
            # Persist the client's model choice across reconnects: the parked
            # session retains whatever model it last used, but the reconnecting
            # client may explicitly want a different one (or the same one it
            # previously chose, recovered from localStorage on page reload).
            # Only override when the client actually sent a model, so a None
            # payload doesn't accidentally reset a previously selected model
            # back to the SDK default.
            if model is not None:
                await parked.set_model(model)  # type: ignore[attr-defined]
            self._current = parked  # type: ignore[assignment]
            await self._current.notify_reconnected()  # type: ignore[union-attr]
            logger.info(
                "session_reattached",
                session_id=session_id,
                connection_id=self._connection_id,
            )
            return self._current

        # ── Slow path: full SDK resume from disk ────────────────────
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
        session = self._factory(
            send=self._send,
            cwd=cwd,
            resume_session_id=session_id,
            auto_approve=auto_approve,
            model=model,
        )
        await session.start()
        self._current = session
        return session

    # ── Pass-throughs to active session ──────────────────────────────

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

    def resolve_user_input(
        self, tool_use_id: str, answers: list[str]
    ) -> bool:
        if self._current is None:
            return False
        return self._current.permissions.resolve_user_input(tool_use_id, answers)

    def resolve_permission(
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool:
        if self._current is None:
            return False
        return self._current.permissions.resolve(
            tool_use_id, allow=allow, reason=reason
        )

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
        """Called on WS disconnect: park the active session.

        The session keeps running so an in-progress agent turn is not
        interrupted. If no client reclaims it within the registry TTL
        it will be stopped automatically.
        """
        if self._current is not None:
            registry.park(self._current)  # type: ignore[arg-type]
            self._current = None
        await self._locks.release_all_by_connection(self._connection_id)

    # ── Internals ────────────────────────────────────────────────────

    async def _stop_current(self) -> None:
        if self._current is not None:
            try:
                await self._current.stop()
            except Exception as e:
                logger.warning("session_stop_error", error=str(e))
            self._current = None

    async def _release_current_lock(self) -> None:
        if self._current is not None:
            await self._locks.release(
                self._current.session_id, self._connection_id
            )

    def _on_lock_revoked_factory(self, session_id: str) -> Any:
        """Build a callback that runs when *this* connection's lock is taken."""

        async def _on_revoked(msg: dict[str, Any]) -> None:
            # Notify the WS client so it can drop to read-only.
            await self._send(msg)
            # Stop the active session if it's the one being revoked.
            if (
                self._current is not None
                and self._current.session_id == session_id
            ):
                await self._stop_current()

        return _on_revoked
