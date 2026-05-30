"""worker-copilot SessionManager - thin wrapper over the shared manager.

See worker-claude/src/sdk/manager.py for the rationale. Identical
shape; we just bind the shared manager to our copilot Session class.
"""

from __future__ import annotations

from worker_shared.locks.manager import LockManager
from worker_shared.sdk.base import SendFn
from worker_shared.sdk.manager import SessionManager as _SharedSessionManager

from src.sdk.session import Session


class SessionManager(_SharedSessionManager):
    """worker-copilot flavor: shared manager parameterized with Session."""

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
