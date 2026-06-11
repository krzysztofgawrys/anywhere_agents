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

from worker_shared.db import db
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
        self._current_project_id: int | None = None

    @property
    def current(self) -> SessionProtocol | None:
        return self._current

    # ── Public lifecycle ─────────────────────────────────────────────

    async def new_session(
        self,
        cwd: str,
        *,
        project_id: int | None = None,
        auto_approve: bool = False,
        model: str | None = None,
        effort: str | None = None,
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

        session = self._factory(
            send=self._send,
            cwd=cwd,
            auto_approve=auto_approve,
            model=model,
            effort=effort,
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

        await self._park_current()
        await session.start()
        self._current = session
        self._current_project_id = project_id
        if project_id is not None:
            await db.upsert_session(
                session.session_id, project_id, model=model, effort=effort,
            )
        return session

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        *,
        project_id: int | None = None,
        force: bool = False,
        auto_approve: bool = False,
        model: str | None = None,
        effort: str | None = None,
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

        # If the client didn't send preferences, check the DB so the
        # session resumes with the same model/effort even across worker
        # restarts or different browsers.
        if model is None or effort is None:
            db_model, db_effort = await db.get_session_prefs(session_id)
            if model is None:
                model = db_model
            if effort is None:
                effort = db_effort

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

            await self._park_current()
            parked.rebind(self._send)  # type: ignore[attr-defined]
            parked.set_auto_approve(auto_approve)  # type: ignore[attr-defined]
            if model is not None:
                await parked.set_model(model)  # type: ignore[attr-defined]
            if effort is not None:
                # Re-apply the resolved effort too - otherwise a reattached
                # parked session keeps its stale in-memory effort and diverges
                # from the persisted/DB value the UI badge reflects.
                await parked.set_effort(effort)  # type: ignore[attr-defined]
            self._current = parked  # type: ignore[assignment]
            self._current_project_id = project_id
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

        await self._park_current()
        session = self._factory(
            send=self._send,
            cwd=cwd,
            resume_session_id=session_id,
            auto_approve=auto_approve,
            model=model,
            effort=effort,
        )
        await session.start()
        self._current = session
        self._current_project_id = project_id
        if project_id is not None:
            await db.upsert_session(
                session_id, project_id, model=model, effort=effort,
            )
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
            if self._current_project_id is not None:
                await db.update_session_field(
                    self._current.session_id, "model", model,
                )

    async def set_effort(self, effort: str | None) -> None:
        if self._current is not None:
            await self._current.set_effort(effort)
            if self._current_project_id is not None:
                await db.update_session_field(
                    self._current.session_id, "effort", effort,
                )

    async def get_context_usage(self) -> dict | None:
        if self._current is not None:
            return await self._current.get_context_usage()
        return None

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

    async def _park_current(self) -> None:
        """Park the active session so it continues running in the background.

        The session is moved to the registry and its lock is released so
        another client (or the same client switching back) can reclaim it.
        The in-progress agent turn keeps running; the registry TTL will
        clean it up eventually if nobody resumes it.
        """
        if self._current is not None:
            sid = self._current.session_id
            registry.park(self._current)
            await self._locks.release(sid, self._connection_id)
            self._current = None

    def _on_lock_revoked_factory(self, session_id: str) -> Any:
        """Build a callback that runs when *this* connection's lock is taken."""

        async def _on_revoked(msg: dict[str, Any]) -> None:
            # Notify the WS client so it can drop to read-only.
            await self._send(msg)
            # Stop the active session if it's the one being revoked -
            # another client is taking over so we terminate (not park).
            if (
                self._current is not None
                and self._current.session_id == session_id
            ):
                try:
                    await self._current.stop()
                except Exception as e:
                    logger.warning("session_stop_error", error=str(e))
                self._current = None

        return _on_revoked
