# Adding a new agent worker

This is the step-by-step recipe for plugging a new agent CLI (Codex,
Aider, Goose, Gemini, OpenCode, ...) into the hub as another worker.

The hard work is already factored into shared modules:

- `worker_shared/sdk/base.py` - `SessionProtocol` + `PermissionsProtocol`
  describing the minimal surface your adapter must expose.
- `worker_shared/sdk/manager.py` - shared `SessionManager` (locks,
  registry parking, fast-path reattach, message routing). You don't
  rewrite this.
- `worker_shared/sdk/push_notify.py` - HTTP-out-of-band push helper.
  Just call it from your event handlers when you want a phone
  notification.
- `worker_shared/sdk/registry.py` - the parking registry. Sessions
  that implement `SessionProtocol` can be parked here on WS disconnect.
- `worker_shared/files/`, `worker_shared/terminal/`,
  `worker_shared/projects/`, `worker_shared/locks/` - file browser,
  PTY, project DB, lock manager. SDK-agnostic.
- `worker_shared/ws/` (effectively `worker-*/src/ws/handler.py` is the
  same shape across workers) - WS routing, ping/pong, terminal proxy,
  upload-over-WS handler.

Your job for a new worker:

1. **A `Session` class** that wraps your CLI / SDK and implements
   `SessionProtocol`. This is where the agent-specific work lives.
2. **A `PermissionBroker` class** implementing `PermissionsProtocol`.
3. **A thin `SessionManager`** that binds the shared manager to your
   `Session` factory.
4. **A `handler.py`** that routes WS messages (largely copy-paste
   from `worker-copilot/src/ws/handler.py`).
5. **A `main.py`** entry point (copy-paste from
   `worker-copilot/src/main.py`, adjust SDK init).
6. **A `Dockerfile`** in `docker/` and a `worker-<name>-compose.yml`
   at the repo root.

Reference implementations: `worker-claude/` and `worker-copilot/`.
Both are <1000 LOC each since most of the heavy lifting is shared.

---

## Walkthrough: hypothetical `worker-codex`

### 1. Package skeleton

```
worker-codex/
├── pyproject.toml          (copy worker-copilot/pyproject.toml, rename)
├── src/
│   ├── __init__.py
│   ├── main.py             (copy from worker-copilot)
│   ├── projects/
│   │   ├── __init__.py
│   │   └── scanner.py      (where does Codex CLI store session history?)
│   ├── sessions/
│   │   ├── __init__.py
│   │   └── reader.py       (parse Codex's session-history file format)
│   ├── sdk/
│   │   ├── __init__.py
│   │   ├── client.py       (Codex SDK singleton, optional)
│   │   ├── manager.py      (5-line wrapper, see step 4 below)
│   │   ├── permissions.py  (PermissionsProtocol impl)
│   │   └── session.py      (SessionProtocol impl - bulk of the work)
│   └── ws/
│       ├── __init__.py
│       └── handler.py      (copy from worker-copilot, drop SDK-specific bits)
```

### 2. `session.py` (the agent adapter)

This is where you actually plug into the Codex CLI. The skeleton:

```python
from worker_shared.sdk.base import SendFn, SessionProtocol  # for type hints
from worker_shared.sdk.push_notify import emit_push_notify

from src.sdk.permissions import PermissionBroker


class Session:  # implements SessionProtocol structurally
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
        ...

    @property
    def session_id(self) -> str: ...
    @property
    def cwd(self) -> str | None: ...
    @property
    def permissions(self) -> PermissionBroker: ...

    async def start(self) -> None:
        # Connect to Codex CLI, register event handler, emit session_started.
        await self._send({"type": "session_started", "payload": {...}})

    async def stop(self) -> None:
        # Disconnect from Codex, cancel background tasks.

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        # Send the prompt to Codex; events come back asynchronously.

    async def interrupt(self) -> None: ...
    def set_auto_approve(self, value: bool) -> None: ...
    async def set_model(self, model: str | None) -> None: ...

    def rebind(self, send: SendFn, *, parked: bool = False) -> None:
        # Just store the new send callback (and the parked flag).
        self._send = send

    async def notify_reconnected(self) -> None:
        await self._send({"type": "session_started", "payload": {...}})
        await self._permissions.resend_pending_user_inputs(self._send)
```

The interesting work is the event handler that translates Codex's
native event stream into the WS messages the frontend already
understands:

| Codex event                | WS message                |
| -------------------------- | ------------------------- |
| Token / text chunk         | `text_delta`              |
| Tool call start            | `tool_call`               |
| Tool result                | `tool_result`             |
| Reasoning chunk            | `thinking`                |
| Permission required        | `permission_request`      |
| Free-text question         | `user_input_request`      |
| Turn finished              | `result`                  |
| Other system event         | `system`                  |

When the agent goes idle waiting on something (permission decision,
free-text answer, end of turn), call `emit_push_notify(...)` so the
user gets a notification on their phone even if the browser is
backgrounded.

### 3. `permissions.py`

```python
from worker_shared.sdk.base import PermissionsProtocol  # for type hints


class PermissionBroker:  # implements PermissionsProtocol structurally
    @property
    def is_auto_approve(self) -> bool: ...
    def set_auto_approve(self, value: bool) -> None: ...

    def resolve(
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool:
        # Resolve the pending future for this tool_use_id.
        # Convert (allow, reason) to whatever your SDK's permission type
        # expects. Return True if matched, False otherwise.
        ...

    def resolve_user_input(
        self, tool_use_id: str, answers: list[str]
    ) -> bool: ...
```

See `worker-claude/src/sdk/permissions.py` for the full reference
including request brokering and post-disconnect re-send logic.

### 4. `manager.py` (literally five lines)

```python
from worker_shared.locks.manager import LockManager
from worker_shared.sdk.base import SendFn
from worker_shared.sdk.manager import SessionManager as _SharedSessionManager

from src.sdk.session import Session


class SessionManager(_SharedSessionManager):
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
```

That's the entire local SessionManager. Locks, parking, prompt
routing, model switching - all handled by the shared base.

### 5. `handler.py`

Copy from `worker-copilot/src/ws/handler.py`. The routing is the
same; the SDK-specific bits are wrapped inside `Session` (step 2)
so the handler doesn't need to know whether you're talking to Claude,
Copilot, or Codex.

### 6. `main.py`

Copy from `worker-copilot/src/main.py`. Adjust:
- `app = FastAPI(title="Claude Web Worker (Codex)", ...)`
- `WORKER_TYPE` default in `_build_dialer()`
- SDK-specific startup hook (if you have a client singleton like
  `worker-copilot/src/sdk/client.py`)

### 7. Dockerfile + compose

Mirror `docker/worker-copilot.Dockerfile` and
`worker-copilot-compose.yml`. The only differences will be:
- Which CLI binary you install in the image (codex / aider / goose).
- Which host directory the CLI uses for auth (e.g. `~/.codex`).
- A different default port if you don't want to share `:8003`.

### 8. Register with the hub

Add an entry to `workers.json`:

```json
{
  "id": "local-codex",
  "type": "codex",
  "label": "Codex (laptop)",
  "url": "ws://worker-codex:8004/ws",
  "secret": "..."
}
```

`type` is free-form. The frontend uses it as a per-project badge and
to pick the right model list for `/model` autocomplete.

Restart the hub once after editing `workers.json` (bind-mount inode
staleness - see CLAUDE.md note about edits to single-file mounts).

### 9. (Optional) Reverse mode

If your worker runs on a different machine and you want zero-ingress
deployment, add the reverse-mode env block from
`worker-copilot-compose.yml` (`WORKER_MODE=inbound`, `HUB_PUBLIC_URL`,
CF Access service token), and set `mode: "inbound"` on the hub side
workers.json entry. The dialer + data-channel infrastructure is
already in place in `worker_shared`.

---

## What CAN you reuse vs what must you write fresh

| Concern | Where it lives | Need new code? |
| --- | --- | --- |
| Hub <-> worker WS protocol | Shared, all set | No |
| Lock coordination | `worker_shared.locks` | No |
| Registry parking | `worker_shared.sdk.registry` | No |
| File browser sandboxing | `worker_shared.files` | No |
| Terminal (PTY) | `worker_shared.terminal` | No |
| Project DB schema | `worker_shared.projects` | No |
| Project scanning | per-worker `projects/scanner.py` | Yes - agent-specific dir layout |
| Session history reader | per-worker `sessions/reader.py` | Yes - agent-specific file format |
| WS handler | per-worker `ws/handler.py` (mostly copy-paste) | Copy + tweak |
| Reverse-mode dialer | `worker_shared` (copy `worker-copilot/src/ws/dialer.py`) | Copy as-is |
| SDK adapter | per-worker `sdk/session.py` + `permissions.py` | Yes - this is the work |
| `SessionManager` | shared, 5-line wrapper in your worker | Tiny shim |
| Push notifications | `worker_shared.sdk.push_notify` | No - just call it |

A new worker is realistically **~500-800 LOC of agent-specific glue**
plus a Dockerfile and a compose file. The hard architectural decisions
(locks, parking, multi-machine federation, security sandbox) are all
already made.
