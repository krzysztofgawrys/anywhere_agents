# Bootstrap auth protocol

Lets a worker container start with **zero agent credentials on disk**,
"dial home" to the hub, and ask the user (through the browser) to
supply credentials on the fly. After bootstrap the credentials live in
the worker's volume; the agent SDK rotates them from there going
forward and the hub stays stateless.

Designed for `flow=api_key` (worker-claude, where Anthropic's SDK
doesn't yet support RFC 8628 device code) and extensible to
`flow=device_code` for workers whose SDK already exposes that
(worker-codex via `Codex().login_with_device_code()`,
worker-copilot via GitHub device flow).

## Motivation

Without bootstrap, deploying a worker on a fresh box requires:

1. Install the agent CLI on the host (`claude login` / `codex login` / ...).
2. Mount `~/.claude` (or `~/.codex` etc.) into the container.
3. Restart container.

That's three host-side steps before the worker can do anything useful,
all coupled to the agent vendor's specific auth flow. With bootstrap:

```bash
docker run -d \
  -e HUB_PUBLIC_URL=https://hub.example.com \
  -e WORKER_SECRET=xxx \
  -e CF_ACCESS_CLIENT_ID=yyy \
  -e CF_ACCESS_CLIENT_SECRET=zzz \
  -v worker-claude-data:/home/app \
  <image>
```

The worker boots, dials the hub, the user gets a notification in their
browser sidebar ("Worker X needs auth - click to set up"), pastes their
API key once, and from then on the worker is fully autonomous. No
host-side CLI install, no manual mounts.

## Message types

All messages travel over the existing worker <-> hub <-> browser WS
channels - the auth flow piggy-backs on the same connections that
carry chat / tool calls / push notifications. No new endpoints needed.

### `auth_needed` (worker -> hub -> browser)

Emitted by the worker when its agent SDK init fails for lack of
credentials.

```json
{
  "type": "auth_needed",
  "payload": {
    "worker_id": "remote-claude",
    "agent_type": "claude",
    "flow": "api_key",
    "request_id": "<uuid>",
    "instructions": "Generate an API key at https://console.anthropic.com/keys and paste it below.",
    "expires_at": 1780200000
  }
}
```

Fields:

- `worker_id`: the workers.json id, so the frontend knows which
  worker is blocked.
- `agent_type`: `claude` / `codex` / `copilot` / ... for icon and
  copy customization in the modal.
- `flow`: `api_key` (paste a key) or `device_code` (open a URL and
  enter a code).
- `request_id`: UUID minted by the worker; reused in `auth_provided`
  to correlate the response back to the right pending future.
- `instructions`: free-text shown above the form, agent-specific
  guidance on where to generate the credential.
- `expires_at`: unix-seconds when the worker will give up waiting and
  emit `auth_status state=expired`.

For `flow=device_code` the payload additionally carries:

```json
"device_code": "ABCD-EFGH",
"verification_url": "https://chatgpt.com/codex/activate",
"verification_url_complete": "https://chatgpt.com/codex/activate?user_code=ABCD-EFGH"
```

The frontend shows the URL + code (or a "Click to open" link to the
complete URL) and updates the user as polling progresses via
`auth_status`.

### `auth_provided` (browser -> hub -> worker)

Emitted by the browser when the user submits the form.

```json
{
  "type": "auth_provided",
  "payload": {
    "worker_id": "remote-claude",
    "request_id": "<uuid>",
    "credentials": {
      "api_key": "sk-ant-..."
    }
  }
}
```

Shape of `credentials` is `flow`-dependent. For `api_key`, just
`{"api_key": "..."}`. For `device_code` the browser doesn't send
credentials - the worker is doing the polling - so `auth_provided`
for that flow only signals "user has acknowledged, please continue".

The hub validates that the browser submitting `auth_provided` is
authenticated (CF Access JWT - same gate as `/ws`), routes it to the
WorkerConnection identified by `worker_id`. Credentials never touch
hub storage; they're forwarded in-memory and discarded.

### `auth_status` (worker -> hub -> browser)

Emitted by the worker to update the frontend on bootstrap progress.

```json
{
  "type": "auth_status",
  "payload": {
    "worker_id": "remote-claude",
    "request_id": "<uuid>",
    "state": "polling" | "completed" | "failed" | "expired",
    "error": "Optional error message when state=failed"
  }
}
```

Broadcast to all browser sessions so multiple tabs / devices stay in
sync. The banner in each browser clears when `state=completed`.

### `auth_cancel` (browser -> hub -> worker, optional)

Emitted when the user dismisses the modal. Worker stops polling and
the session attempt fails cleanly.

```json
{
  "type": "auth_cancel",
  "payload": {
    "worker_id": "remote-claude",
    "request_id": "<uuid>"
  }
}
```

## Worker-side state machine

```
                                +-----------+
                                |  start()  |
                                +-----+-----+
                                      |
                  +-------------------+-------------------+
                  |                                       |
        credentials present                       credentials missing
                  |                                       |
                  v                                       v
         +-----------------+                  +---------------------+
         | SDK init normal |                  | emit auth_needed    |
         +-----------------+                  |  + start poll loop  |
                  |                           +----------+----------+
                  v                                      |
            session ready                                v
                                              +------------------+
                                              | wait auth_provided|
                                              |  OR timeout       |
                                              +---+---------+----+
                                                  |         |
                                          provided           expired/cancel
                                                  |         |
                                                  v         v
                                          +-------------+   +-----------+
                                          | save creds  |   | emit fail |
                                          | retry SDK   |   | abort     |
                                          +-------------+   +-----------+
                                                  |
                                                  v
                                            session ready
```

## Persistence

Bootstrap credentials are written to the worker volume under a stable
path so SDK reuse them on subsequent container starts without
re-prompting:

```
/home/app/.claude-web/credentials/<agent_type>.json
```

Shape:

```json
{
  "agent_type": "claude",
  "flow": "api_key",
  "data": {"api_key": "sk-ant-..."},
  "stored_at": "2026-05-30T20:15:00Z"
}
```

On worker start the credentials store is read first. If present, the
worker:

- For `api_key` flow: sets the matching env var (e.g.
  `ANTHROPIC_API_KEY`) and skips bootstrap entirely.
- For `device_code` flow: hands the stored refresh token to the SDK;
  the SDK auto-rotates as it always does.

File perms `0600` to keep secrets out of accidentally-mounted reads.

## Trust model

What sees credentials and when:

- **Browser**: types / pastes the credential. JavaScript memory only,
  sent to hub over the same authenticated CF Access channel as
  everything else.
- **Hub**: forwards in-memory. Does NOT persist. Logs strip the
  credential field.
- **Worker**: persists to its own volume. SDK reads from there.

What does NOT see credentials:

- Other workers (one bootstrap per worker_id).
- The hub's disk / SQLite / logs.
- Any client other than the originating browser session for the
  duration of the round-trip (`auth_provided` is unicast to the
  named worker, not broadcast).

The `auth_needed` / `auth_status` events ARE broadcast (so a user
with multiple browser tabs sees the prompt everywhere); only the
`auth_provided` payload travels through the WS and is unicast.

## Extending to other flows

Adding `flow=device_code` for worker-codex:

1. Worker calls `Codex().login_with_device_code(callback=on_code)`.
2. The callback receives `(device_code, verification_url, ...)` from
   the SDK and emits `auth_needed` with those fields filled in.
3. SDK polls Anthropic / OpenAI in the background.
4. On success the SDK writes its own credentials to `~/.codex/`;
   worker emits `auth_status state=completed`.

Browser-side: same modal component, just renders the URL + code
prominently instead of an input field.

Adding a custom flow (e.g. Aider API key in env file): set
`flow=api_key` with custom `instructions` text and a different
persistence target on the worker side.
