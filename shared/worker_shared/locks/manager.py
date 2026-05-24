"""Per-session lock manager.

Locks are session_id → owner. They protect against two clients (e.g. phone +
laptop) editing the same conversation simultaneously. A lock is acquired when a
client calls new_session / resume_session and released on WS disconnect or
explicit release. A new client can force-takeover the lock; the previous
holder receives `lock_revoked` and must drop to read-only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger()


NotifyFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class LockEntry:
    session_id: str
    connection_id: str
    device_label: str
    locked_at: float
    notify: NotifyFn  # called when the lock is revoked


class LockManager:
    """In-memory registry of session locks."""

    def __init__(self) -> None:
        self._locks: dict[str, LockEntry] = {}
        self._mu = asyncio.Lock()

    async def try_acquire(
        self,
        *,
        session_id: str,
        connection_id: str,
        device_label: str,
        notify: NotifyFn,
        force: bool = False,
    ) -> tuple[bool, LockEntry | None]:
        """Try to acquire a lock on `session_id`.

        Returns (acquired, existing_entry).
        - acquired=True: caller owns the lock now.
        - acquired=False: lock held by someone else; existing_entry describes them.

        If `force=True` and another client holds the lock, that client is notified
        with `lock_revoked` and the lock is reassigned to the caller.
        """
        async with self._mu:
            existing = self._locks.get(session_id)
            prior_other: LockEntry | None = None
            if existing and existing.connection_id != connection_id:
                # WS-reconnect race: the old connection's WS may still be
                # waiting on its heartbeat timeout before the worker tears it
                # down and releases the lock. If the new requester is the same
                # logical client (same `email @ device` label) we treat it as
                # a silent takeover rather than surfacing the "Take over"
                # dialog to the user - it's the same person on the same device
                # reclaiming their own session.
                same_client = existing.device_label == device_label
                if not force and not same_client:
                    return False, existing
                prior_other = existing
                logger.info(
                    "lock_revoked",
                    session_id=session_id,
                    from_connection=existing.connection_id,
                    to_connection=connection_id,
                    reason="reconnect" if same_client else "force",
                )
                try:
                    await existing.notify({
                        "type": "lock_revoked",
                        "payload": {"session_id": session_id},
                    })
                except Exception as e:
                    logger.warning("lock_revoke_notify_error", error=str(e))

            entry = LockEntry(
                session_id=session_id,
                connection_id=connection_id,
                device_label=device_label,
                locked_at=time.time(),
                notify=notify,
            )
            self._locks[session_id] = entry
            # `existing` represents who *previously* held the lock (if a different
            # connection); None when the lock was free or held by us already.
            return True, prior_other

    async def release(self, session_id: str, connection_id: str) -> bool:
        """Release a lock only if owned by `connection_id`. Returns True if released."""
        async with self._mu:
            existing = self._locks.get(session_id)
            if existing and existing.connection_id == connection_id:
                del self._locks[session_id]
                return True
            return False

    async def release_all_by_connection(self, connection_id: str) -> list[str]:
        """Release every lock held by `connection_id`. Returns released session ids."""
        async with self._mu:
            released = [
                sid for sid, e in self._locks.items() if e.connection_id == connection_id
            ]
            for sid in released:
                del self._locks[sid]
            return released

    def info(self, session_id: str) -> LockEntry | None:
        return self._locks.get(session_id)

    @property
    def active_locks(self) -> int:
        return len(self._locks)


# Module-level singleton
locks = LockManager()
