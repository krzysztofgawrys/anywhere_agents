# Adding a new agent worker

Step-by-step recipe for plugging a new agent CLI (Codex, Aider, Goose,
Gemini, OpenCode, ...) into the hub as another worker.

The hard architectural work is already factored into shared modules:

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
  `worker_shared/projects/`, `worker_shared/locks/` - file browser
  (sandboxed), PTY, project DB, lock manager. SDK-agnostic.

What you write for a new worker:

1. **A `Session` class** that wraps your CLI / SDK and implements
   `SessionProtocol`. This is where the agent-specific work lives.
2. **A `PermissionBroker` class** implementing `PermissionsProtocol`.
   Stub allowed when your SDK doesn't expose programmatic approvals.
3. **A thin `SessionManager`** that binds the shared manager to your
   `Session` factory. ~30 lines.
4. **A `handler.py`** that routes WS messages (largely copy-paste
   from `worker-copilot/src/ws/handler.py`).
5. **A `dialer.py`** for reverse mode (literally `cp` from
   `worker-copilot/src/ws/dialer.py`; pure transport, no SDK).
6. **A `main.py`** entry point (copy-paste from
   `worker-copilot/src/main.py`, adjust title + WORKER_TYPE default).
7. **A `Dockerfile`** in `docker/` and a `worker-<name>-compose.yml`
   at the repo root.

Reference implementations in the repo:

| Worker | SDK | Permission callback | Token streaming | Session history reader |
| --- | --- | --- | --- | --- |
| `worker-claude/` | `claude-agent-sdk` | Yes (`can_use_tool`) | Yes (partial messages) | Full (`~/.claude/projects/*.jsonl`) |
| `worker-copilot/` | `github-copilot-sdk` | Yes (`on_permission_request`) | Yes (delta events) | Full (events.jsonl per workspace) |
| `worker-codex/` | `openai-codex-sdk` | No (stub) | No (whole items only) | Stub (SDK schema not frozen) |

`worker-codex/` is the reference for the "SDK is missing some features"
case - the protocol is permissive about which features an adapter
actually implements, and `worker-codex/` documents what to do when
your SDK doesn't expose every hook the other two do.

---

## Walkthrough: real `worker-codex`

This worker ships in the repo as the first concrete implementation
built off the shared SDK base. Use it as a reference when writing
your own. Every snippet below corresponds to a file in
`worker-codex/`.

### 1. Package skeleton

```
worker-codex/
├── pyproject.toml             dependency on openai-codex-sdk + worker-shared
├── src/
│   ├── __init__.py
│   ├── main.py                FastAPI entry, WORKER_MODE switch
│   ├── projects/
│   │   ├── __init__.py
│   │   └── scanner.py         scans ~/.codex/sessions/ for project roots
│   ├── sessions/
│   │   ├── __init__.py
│   │   └── reader.py          list_sessions + get_session_messages
│   ├── sdk/
│   │   ├── __init__.py
│   │   ├── client.py          process-wide Codex singleton
│   │   ├── manager.py         ~30-line shim over the shared SessionManager
│   │   ├── permissions.py     PermissionBroker (stub variant)
│   │   └── session.py         the bulk of the agent-specific work
│   └── ws/
│       ├── __init__.py
│       ├── handler.py         copy from worker-copilot, drop list_models
│       └── dialer.py          copy from worker-copilot as-is
```

### 2. `pyproject.toml`

Copy from `worker-copilot/pyproject.toml`, change `name`,
`description`, and swap the agent SDK dependency:

```toml
[project]
name = "claude-web-worker-codex"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "websockets>=13.0",
    "structlog>=24.4.0",
    "aiosqlite>=0.20.0",
    "httpx>=0.27.0",
    "openai-codex-sdk>=0.1.0",   # <-- your agent's SDK
    "worker-shared",
]

[tool.uv.sources]
worker-shared = { path = "../shared", editable = true }
```

### 3. `sdk/client.py` (optional singleton)

If your SDK has a process-wide client / connection pool, wrap it in a
singleton with lazy start. The pattern handles "SDK isn't installed in
the image" gracefully so a misconfigured deployment doesn't crashloop
the container - it boots fine, `/health` stays green, only `new_session`
returns a clear "SDK unavailable" error.

```python
try:
    from openai_codex_sdk import Codex
    _SDK_IMPORT_ERROR: str | None = None
except Exception as _exc:
    Codex = None
    _SDK_IMPORT_ERROR = str(_exc)


def sdk_available() -> bool:
    return Codex is not None


async def get_client() -> object:
    global _client
    if _client is None:
        if Codex is None:
            raise RuntimeError(
                f"SDK not installed: {_SDK_IMPORT_ERROR}"
            )
        _client = Codex()
    return _client
```

See `worker-codex/src/sdk/client.py` for the full version.
`worker-copilot/src/sdk/client.py` shows a richer variant that
spawns a subprocess and stops cleanly on lifespan shutdown.

### 4. `sdk/session.py` (the agent adapter)

This is where the real work happens. Implement `SessionProtocol`
against your SDK. The required surface:

```python
from worker_shared.sdk.base import SendFn, SessionProtocol
from worker_shared.sdk.push_notify import emit_push_notify

from src.sdk.permissions import PermissionBroker


class Session:  # structurally implements SessionProtocol
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
        self._permissions = PermissionBroker()
        self._permissions.set_auto_approve(auto_approve)
        ...

    # ── Properties ──────────────────────────────────────────────
    @property
    def session_id(self) -> str: ...
    @property
    def cwd(self) -> str | None: ...
    @property
    def permissions(self) -> PermissionBroker: ...

    # ── Lifecycle ───────────────────────────────────────────────
    async def start(self) -> None:
        # Connect to your SDK, then announce the session to the WS:
        await self._send({
            "type": "session_started",
            "payload": {
                "session_id": self._session_id,
                "cwd": self._cwd,
                "resumed": bool(self._resume),
                "auto_approve": self._permissions.is_auto_approve,
            },
        })

    async def stop(self) -> None:
        # Cancel background tasks, disconnect SDK, drop references.

    async def send_prompt(
        self,
        text: str,
        *,
        auto_approve_once: bool = False,
        images: list[dict[str, str]] | None = None,
        stream: bool = False,
    ) -> None:
        # Forward to your SDK. Spawn a background consumer that
        # translates SDK events into WS messages (see below).

    async def interrupt(self) -> None: ...
    def set_auto_approve(self, value: bool) -> None: ...
    async def set_model(self, model: str | None) -> None: ...

    # ── Park / reconnect ────────────────────────────────────────
    def rebind(self, send: SendFn, *, parked: bool = False) -> None:
        self._send = send
        self._is_parked = parked

    async def notify_reconnected(self) -> None:
        await self._send({"type": "session_started", "payload": {...}})
        await self._permissions.resend_pending_user_inputs(self._send)
```

The interesting part is translating your SDK's native event stream
into the WS messages the frontend already understands. Mapping table:

| Your SDK event | WS message type | Notes |
| --- | --- | --- |
| Text / token chunk | `text_delta` | Concatenated by frontend within an assistant turn |
| Reasoning / thinking | `thinking` | Optional - drop if SDK doesn't surface |
| Tool call start | `tool_call` | Carries name + input |
| Tool result | `tool_result` | Carries output + is_error |
| Permission request | `permission_request` | Forwarded via `broker.request(...)` |
| Free-text question | `user_input_request` | Forwarded via `broker.request_user_input(...)` |
| Turn finished | `result` | One per send_prompt |
| Anything else | `system` | Catch-all with subtype + opaque data |

When the agent goes idle waiting on something (permission decision,
free-text answer, end of turn), call
`emit_push_notify(title=..., body=...)` so the user gets a phone
notification even if their browser is backgrounded.

**Defensive event mapping** (worker-codex pattern). When the SDK
doesn't publish a stable typed schema, introspect each event by
attribute presence and log unrecognized ones at debug level:

```python
async def _dispatch(self, event: object) -> None:
    etype = getattr(event, "type", None)
    if etype == "turn.completed":
        await self._finish_turn(event=event)
        return
    if etype != "item.completed":
        logger.debug("codex_unknown_event", etype=etype)
        return

    item = getattr(event, "item", None)
    item_type = getattr(item, "type", None) or getattr(item, "kind", None)

    text = getattr(item, "text", None) or getattr(item, "content", None)
    if item_type in {"assistant_message", "message"} and isinstance(text, str) and text:
        await self._send({"type": "text_delta", "payload": {...}})
        return

    tool_name = getattr(item, "tool_name", None) or getattr(item, "name", None)
    if tool_name:
        await self._send({"type": "tool_call", "payload": {...}})
        await self._send({"type": "tool_result", "payload": {...}})
        return

    # Fallback: dump as system event for visibility
    attrs = [a for a in dir(item) if not a.startswith("_")]
    logger.debug("unmapped_item", item_type=item_type, attrs=attrs[:20])
    await self._send({"type": "system", "payload": {...}})
```

This pattern is robust against SDK upgrades - new event kinds show up
in debug logs with their attribute names, making the mapping update a
matter of "run once, read logs, add a branch."

### 5. `sdk/permissions.py`

The broker bridges your SDK's approval flow into the WS Allow / Deny
round-trip. Two variants depending on what your SDK exposes:

**Full variant** (when your SDK has a `can_use_tool`-style callback):
see `worker-claude/src/sdk/permissions.py`. Holds a dict of pending
`asyncio.Future`s keyed by `tool_use_id`, sends `permission_request`
to the frontend, blocks the SDK callback until the future resolves.

**Stub variant** (when your SDK gates approvals via config, not
callbacks - the worker-codex case): `resolve()` is a no-op returning
False; `is_auto_approve` / `set_auto_approve` still work so the
frontend's per-project flag is recorded even if it has no effect
inside the agent. Document the limitation prominently in the module
docstring and the compose file so operators don't expect the
frontend's Allow / Deny UI to fire.

Either way, the interface that `worker_shared.sdk.manager`
consumes is uniform:

```python
class PermissionBroker:
    @property
    def is_auto_approve(self) -> bool: ...
    def set_auto_approve(self, value: bool) -> None: ...
    def arm_one_shot(self) -> None: ...
    def disarm_one_shot(self) -> None: ...

    def resolve(
        self, tool_use_id: str, *, allow: bool, reason: str = ""
    ) -> bool: ...

    def resolve_user_input(
        self, tool_use_id: str, answers: list[str]
    ) -> bool: ...

    async def request_user_input(
        self, send, session_id, tool_use_id, questions
    ) -> list[str]: ...

    async def resend_pending_user_inputs(self, send) -> None: ...

    def cancel_permissions(self, reason: str = "...") -> None: ...
    def cancel_all(self, reason: str = "...") -> None: ...
```

Worker-claude's broker also converts `(allow, reason)` into the
SDK-specific `PermissionResultAllow` / `PermissionResultDeny` types
inside `resolve()` - that's the right place for SDK-specific type
conversion. The shared `SessionManager.resolve_permission()` always
calls `broker.resolve(id, allow=..., reason=...)` with the uniform
signature.

### 6. `sdk/manager.py` (literally a 30-line subclass)

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

That's the whole local SessionManager. Locks, parking, prompt routing,
model switching, permission resolution - all in the shared base.
Keeping this thin subclass means `ws/handler.py` can stay non-generic:
`from src.sdk.manager import SessionManager` reads the same in every
worker, no special-cased factory wiring per call site.

### 7. `projects/scanner.py` (where does your CLI keep state?)

Each agent CLI stores session metadata somewhere different:

- `worker-claude` -> `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`
- `worker-copilot` -> `~/.copilot/session-state/<uuid>/workspace.yaml`
- `worker-codex` -> `~/.codex/sessions/<thread_id>/{metadata,workspace,state,session}.json`

For each session directory, find the `cwd` field (variously named
`working_directory`, `workspace_path`, `cwd`, `project_root`), group
by cwd, and upsert one project row per distinct cwd into the shared
SQLite via `worker_shared.db.Database`. Defensive: if the metadata
schema isn't documented yet, walk all candidate filename / fieldname
variants and skip silently when none parse. Worker stays healthy on
a fresh install with no sessions yet.

See `worker-codex/src/projects/scanner.py` for the defensive variant
(walks `metadata.json` / `workspace.json` / `state.json` /
`session.json` and tries multiple field names for cwd + mtime).

### 8. `sessions/reader.py` (chat history backfill)

Two functions, called by the shared WS handler:

```python
def list_sessions(project_path: str) -> list[dict[str, Any]]:
    # [{"session_id", "summary", "created_at", "updated_at"}, ...]

def get_session_messages(
    project_path: str,
    session_id: str,
    *,
    limit: int = 30,
    before_uuid: str | None = None,
) -> dict[str, Any]:
    # {"messages": [...], "has_more": bool, "oldest_uuid": str | None}
```

If your SDK doesn't have a stable on-disk transcript format yet, ship
a stub that returns an empty `messages` list with `has_more=False`
plus one `system` notice message explaining the limitation. The
frontend will show the notice instead of a blank chat with no
explanation - see `worker-codex/src/sessions/reader.py` for the
template.

### 9. `ws/handler.py` and `ws/dialer.py`

```bash
cp worker-copilot/src/ws/handler.py worker-<name>/src/ws/handler.py
cp worker-copilot/src/ws/dialer.py  worker-<name>/src/ws/dialer.py
```

`dialer.py` is pure transport (control + data channels for reverse
mode); copy as-is.

`handler.py` needs at most one tweak: if your SDK doesn't have
`list_models()`, replace the worker-copilot branch with a one-liner:

```python
if msg_type == "list_models":
    await send({"type": "models", "payload": {"models": []}})
    return terminal
```

Hub will fall back to its type-default list. To populate `/model`
autocomplete for your worker type, add an entry to
`hub/src/ws/handler.py:_DEFAULT_MODELS_BY_TYPE`.

### 10. `main.py`

```bash
cp worker-copilot/src/main.py worker-<name>/src/main.py
```

Then in that copy, change:
- The module docstring header to `worker-<name>`.
- `app = FastAPI(title="Claude Web Worker (<Name>)", ...)`
- The `WORKER_TYPE` default in `_build_dialer()` and in the inbound-mode
  docstring (used as the `type` field when the worker registers with
  the hub in reverse mode).
- Any SDK-specific startup or shutdown hook (worker-copilot's
  `stop_client()` for example).

### 10b. (Optional) env-var-bypass for headless deployments

For CI / Kubernetes / Vault deployments, expose an agent-specific
API key env var that lets users skip both the host-side login and
the bootstrap browser modal. Pattern:

```python
# in your worker's sdk/session.py or sdk/client.py
def my_has_credentials() -> bool:
    if os.environ.get("MY_AGENT_API_KEY"):
        return True
    # ... fall through to persisted bootstrap blob, host login, etc.
```

```yaml
# worker-<name>-compose.yml
environment:
  - MY_AGENT_API_KEY=${MY_AGENT_API_KEY:-}
```

Document the env var name in `docs/bootstrap-auth-protocol.md`'s
"When NOT to use bootstrap" table. Same container image then works
for interactive (modal flow) AND headless (just-inject-env) deployments
without rebuilding.

### 11. Dockerfile

Mirror `docker/worker-copilot.Dockerfile`. Key per-worker decisions:

- **Which port to expose**. Convention so far: claude=8002,
  copilot=8003, codex=8004. Pick the next free integer; the hub
  doesn't care, it dials whatever URL the workers.json entry says.
- **Whether to pre-install a CLI binary** during build. Worker-claude
  installs Node + `@anthropic-ai/claude-code`. Worker-copilot ships
  the binary inside the SDK wheel (nothing extra). Worker-codex calls
  `Codex.install()` at build time to pre-fetch the Rust binary so
  cold starts don't have to. If the install can fail (network blip
  during build), wrap it in `|| echo "fallback ..."` so the image
  builds anyway and the SDK can retry lazily on first session.
- **Auth volume**. Pick a name (`CODEX_HOME`, `COPILOT_HOME`,
  `CLAUDE_HOME`, ...) that mirrors how the underlying CLI looks for
  credentials. The compose file then bind-mounts `~/.<name>` from the
  host into the container at the same path.

### 12. `worker-<name>-compose.yml` (standalone for remote machines)

Copy `worker-copilot-compose.yml`. Document both server and inbound
modes in the file header. Document any caveats specific to your
worker (e.g. worker-codex documents the no-programmatic-permissions
limitation right in the compose comment so operators see it before
deploying).

### 13. Register with the hub

Append to `workers.json`:

```json
{
  "id": "local-<name>",
  "type": "<name>",
  "label": "<friendly label>",
  "url": "ws://worker-<name>:<port>/ws",
  "secret": "..."
}
```

`type` is free-form. The frontend renders it as a per-project badge
and uses it to look up the model list for `/model` autocomplete.

**Bind-mount staleness gotcha**: editing `workers.json` from outside
the container (with `Write` from a tool, with `:wq` from Vim, with
`mv tmp workers.json`) changes the file's inode. The hub container's
mount still points at the old inode and serves stale content. The fix
is `docker compose restart hub`. If you're editing frequently, use
`cat > workers.json <<'EOF' ... EOF` which truncates in place and
preserves the inode.

### 14. (Optional) Add to root `compose.yml` under a profile

If the new worker depends on a one-time setup step that operators
haven't necessarily done (e.g. `codex login`, `aider --setup`,
manual SSH key provisioning), gate it behind a Docker Compose
profile so `docker compose up -d` doesn't start it by default:

```yaml
worker-<name>:
  profiles: ["<name>"]
  build:
    context: .
    dockerfile: docker/worker-<name>.Dockerfile
  ...
```

Operators opt in with:

```bash
docker compose --profile <name> up -d --build worker-<name>
```

Worker-codex uses this pattern (`profiles: ["codex"]`). Worker-claude
and worker-copilot are unconditional because their auth setup is
benign (an empty `~/.claude/` or `~/.copilot/` doesn't break anything
- the agent just complains at first session, not at container start).

### 15. (Optional) Reverse mode

The dialer copied from worker-copilot in step 9 already handles this.
To switch a deployed worker into reverse mode:

- Worker `.env`: `WORKER_MODE=inbound`, `HUB_PUBLIC_URL=https://...`,
  `WORKER_ID=...`, and CF Access service token credentials if the
  hub sits behind CF Access.
- Hub `workers.json` entry: `"mode": "inbound"`, omit `url`.
- Restart hub (bind-mount staleness on workers.json).

See `docs/reverse-mode-testing.md` for the end-to-end test recipe.

---

## What CAN you reuse vs what must you write fresh

| Concern | Where it lives | Need new code? |
| --- | --- | --- |
| Hub <-> worker WS protocol | Shared | No |
| Lock coordination | `worker_shared.locks` | No |
| Registry parking | `worker_shared.sdk.registry` | No |
| File browser sandboxing | `worker_shared.files` | No |
| Terminal (PTY) | `worker_shared.terminal` | No |
| Project DB schema | `worker_shared.projects` | No |
| `SessionManager` | shared, 30-line subclass in your worker | Tiny shim |
| Reverse-mode dialer | `worker-copilot/src/ws/dialer.py` | Copy as-is |
| WS handler | `worker-copilot/src/ws/handler.py` | Copy + one tweak (list_models) |
| `main.py` | `worker-copilot/src/main.py` | Copy + title/type tweaks |
| Push notifications | `worker_shared.sdk.push_notify` | No - just call it |
| Project scanning | per-worker `projects/scanner.py` | Yes - agent-specific dir layout |
| Session history reader | per-worker `sessions/reader.py` | Yes - or ship a stub |
| SDK adapter (Session class) | per-worker `sdk/session.py` | Yes - this is the work |
| PermissionBroker | per-worker `sdk/permissions.py` | Yes - or stub variant |
| Dockerfile + compose | per-worker | Yes - but mostly templated |

## What "the work" looks like, by SDK shape

| Your SDK exposes... | Adapter complexity | Reference |
| --- | --- | --- |
| Per-tool-call permission callback + token streaming + typed events | Highest | `worker-claude/` |
| Permission callback + delta events + typed events | High | `worker-copilot/` |
| Whole-item events + no permission callback + no streaming | Medium | `worker-codex/` |
| Just a stdin/stdout subprocess with text frames | Higher than worker-codex (build the streaming + permission flow yourself) | Open question |

A new worker against a comparably-typed SDK lands somewhere between
**~700 LOC** (worker-codex shape - permission stub, streaming stub,
history stub) and **~1500 LOC** (worker-claude shape - full
Monitor/TaskCreate task-event handling, multimodal streaming, model
switching, etc.). The architectural primitives (locks, parking,
multi-machine federation, security sandbox) are all already made and
don't show up in that count.
