"""worker-claude SessionManager - thin wrapper over the shared manager.

The shared SessionManager (worker_shared.sdk.manager) handles all the
generic lifecycle bits (locks, registry parking, fast-path reattach,
WS message routing). Worker-claude only needs to bind it to its own
concrete Session factory.

Keeping this wrapper preserves the call sites in src/ws/handler.py
(`SessionManager(send, connection_id=..., device_label=...)`) so the
refactor is non-breaking for the handler.
"""

from __future__ import annotations

from worker_shared.locks.manager import LockManager
from worker_shared.sdk.base import SendFn
from worker_shared.sdk.manager import SessionManager as _SharedSessionManager

from src.sdk.session import Session


class SessionManager(_SharedSessionManager):
    """worker-claude flavor: shared manager parameterized with Session."""

    def __init__(
        self,
        send: SendFn,
        *,
        connection_id: str,
        device_label: str = "unknown",
        lock_manager: LockManager | None = None,
    ) -> None:
        super().__init__(
            send=send,
            session_factory=Session,
            connection_id=connection_id,
            device_label=device_label,
            lock_manager=lock_manager,
        )
