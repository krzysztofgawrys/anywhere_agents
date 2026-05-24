"""Stub SessionManager - not used yet by the skeleton ws/handler.

Phase 4 will route prompt/new_session/resume_session through this manager,
exactly like worker-claude/src/sdk/manager.py does today. Kept as a stub so
the import graph mirrors worker-claude.
"""

from __future__ import annotations

from typing import Any

from src.sdk.session import SendFn, Session


class SessionManager:
    """Placeholder for the SDK session lifecycle owner. No-ops in skeleton."""

    def __init__(
        self,
        send: SendFn,
        *,
        connection_id: str,
        device_label: str = "unknown",
    ) -> None:
        self._send = send
        self._connection_id = connection_id
        self._device_label = device_label
        self._current: Session | None = None

    @property
    def current(self) -> Session | None:
        return self._current

    async def stop(self) -> None:
        return None
