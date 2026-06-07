"""Protocol contracts for an agent session adapter.

Each worker (worker-claude, worker-copilot, future worker-codex / -aider
/ -goose / ...) implements ONE concrete Session class against the
`SessionProtocol` here. Everything else - lock management, registry
parking, the SessionManager that wraps these sessions and serves them
to the WS handler - lives in `worker_shared.sdk.manager` and is reused
verbatim across workers.

Why this layout: before this split, worker-claude and worker-copilot
shipped near-identical SessionManager classes (~500 LOC of duplication
between the two). Adding a third worker meant copy-paste again. With
the Protocol, a new agent is roughly:

  worker-<agent>/src/sdk/
    session.py       - implements SessionProtocol against the agent's SDK
    permissions.py   - implements PermissionsProtocol (agent-specific
                       permission/user-input shape)
    client.py        - any agent-specific singleton (optional)

  worker-<agent>/src/main.py   - mirrors worker-copilot/main.py
  worker-<agent>/src/ws/handler.py - mirrors the existing handler

See docs/adding-a-worker.md for the full walkthrough.

Type discipline: we use structural `typing.Protocol` rather than ABCs
so adapters don't need to inherit anything - they just provide the
named methods. That keeps SDK-specific base classes out of the worker
codebases.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

# A WS send callback. Each worker's handler hands one of these to the
# Session at construction time; the Session uses it to push WS messages
# to the frontend (via the hub).
SendFn = Callable[[dict[str, Any]], Awaitable[None]]


@runtime_checkable
class PermissionsProtocol(Protocol):
    """The PermissionBroker shape SessionManager expects.

    Adapters are free to add more methods (e.g. SDK-specific permission
    type conversions); these are the only ones the shared manager
    touches.
    """

    @property
    def is_auto_approve(self) -> bool: ...

    def set_auto_approve(self, value: bool) -> None: ...

    def resolve(
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool:
        """Resolve a pending permission_request.

        Returns True if there was a matching pending request, False if
        the id is unknown (e.g. expired or already answered).
        """
        ...

    def resolve_user_input(
        self, tool_use_id: str, answers: list[str]
    ) -> bool:
        """Resolve a pending user_input_request (one or more answers).

        Returns True on success, False when the id is unknown.
        """
        ...


@runtime_checkable
class SessionProtocol(Protocol):
    """One live agent conversation, bound to a project cwd.

    Implementations own the SDK lifecycle (start/stop), drive the SDK's
    event stream, and translate SDK events into the neutral WS messages
    the frontend already understands (`text_delta`, `thinking`,
    `tool_call`, `tool_result`, `result`, `system`, `task_event`).

    The shared SessionManager calls only the methods declared here;
    SDK-specific surface (e.g. claude's Monitor/TaskCreate tail tasks,
    copilot's per-event blob attachments) stays private to the adapter.
    """

    # ── Identity / introspection ────────────────────────────────────

    @property
    def session_id(self) -> str: ...

    @property
    def cwd(self) -> str | None: ...

    @property
    def permissions(self) -> PermissionsProtocol: ...

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to the SDK, emit `session_started`, begin streaming."""
        ...

    async def stop(self) -> None:
        """Disconnect from the SDK, cancel background tasks, clean up."""
        ...

    async def interrupt(self) -> None:
        """Cancel the in-flight agent turn, if any."""
        ...

    # ── Prompt + model control ──────────────────────────────────────

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        """Send a user prompt.

        `images` items are {"media_type": "image/png|jpeg|gif|webp",
        "data_b64": "..."}. Adapters convert to whatever blob shape the
        underlying SDK wants.

        `stream`: when True, forward fine-grained text/thinking deltas
        as the model produces them. When False, hold deltas back and
        emit whole text/thinking blocks at message boundaries. Behavior
        is per-prompt (toggle from the frontend per send).

        `auto_approve_once`: when True, every permission request for
        THIS prompt is auto-allowed. Resets on `result`.
        """
        ...

    def set_auto_approve(self, value: bool) -> None: ...

    async def set_model(self, model: str | None) -> None:
        """Switch the model mid-session. No-op if the SDK doesn't support it."""
        ...

    async def set_effort(self, effort: str | None) -> None:
        """Change the effort/reasoning level. Implementation varies per SDK."""
        ...

    # ── Park / reconnect (called by manager on WS lifecycle) ────────

    def rebind(self, send: SendFn, *, parked: bool = False) -> None:
        """Swap the WS send callback after disconnect / reconnect.

        Called by the manager when parking the session (after WS drop)
        and again when a reconnecting client reclaims it.
        """
        ...

    async def notify_reconnected(self) -> None:
        """Re-announce the live session to a reconnecting WS client.

        Called by the manager after `rebind(parked=False)`. Should
        re-emit `session_started` and re-send any user_input_request
        the agent is still blocked on (so the user can answer on the
        fresh connection).
        """
        ...


# Concrete session classes constructed by the manager. Manager doesn't
# know the constructor signature, so each worker passes a factory:
SessionFactory = Callable[..., SessionProtocol]
