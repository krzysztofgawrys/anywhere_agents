"""Stub Session for the worker-copilot skeleton.

Satisfies the structural Protocol expected by worker_shared.sdk.registry so
this module can be parked/registered the same way as worker-claude's Session,
but does not actually drive a CopilotClient yet. Phase 4 will replace this
with a real wrapper around `github-copilot-sdk`'s CopilotClient + CopilotSession.

Kept here so that the skeleton's import graph mirrors worker-claude precisely;
when phase 4 lands, only this file (plus permissions.py / manager.py) grows.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class _NoopPermissions:
    """Minimal PermissionBroker stub matching shared registry's Protocol."""

    def cancel_permissions(self, reason: str = "Connection closed") -> None:
        return None


class Session:
    """Placeholder Copilot session - holds an id, swallows lifecycle calls.

    Phase 4 will replace this with a real CopilotClient/CopilotSession wrapper
    that streams events back through ``send``.
    """

    def __init__(
        self,
        send: SendFn,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        auto_approve: bool = False,
        model: str | None = None,
    ) -> None:
        self._send = send
        self._cwd = cwd
        self._session_id = resume_session_id or session_id or str(uuid.uuid4())
        self._model = model
        self._auto_approve = auto_approve
        self.permissions: _NoopPermissions = _NoopPermissions()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cwd(self) -> str | None:
        return self._cwd

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def rebind(self, send: SendFn, *, parked: bool = False) -> None:
        self._send = send

    def set_auto_approve(self, value: bool) -> None:
        self._auto_approve = value

    async def set_model(self, model: str | None) -> None:
        self._model = model
