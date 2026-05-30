# Agents Anywhere

**Self-hosted federation for AI coding agents.** Run Claude Code on your
laptop, GitHub Copilot CLI on your home server, another worker inside a
locked-down VPC, and reach all of them from a single web/mobile UI you
control. No vendor in the data path. Your code never leaves your
infrastructure.

Installable as a PWA on Android / iOS. Exposed to the public internet
(optionally) via Cloudflare Tunnel + Cloudflare Access JWT auth, or kept
fully air-gapped on an internal network.

---

## Why this exists

There are already plenty of web wrappers around `claude` and `copilot` CLIs.
What none of them do is let you point one UI at workers running on multiple
machines that you own, with no third party in the middle.

| Solution                       | Where the hub runs | Where workers run            | Code transits a third party? | Multi-machine federation |
| ------------------------------ | ------------------ | ---------------------------- | ---------------------------- | ------------------------ |
| CloudCLI Cloud                 | Vendor SaaS        | Your box, through their tunnel | Yes                        | No                       |
| CloudCLI (self-host)           | Your box           | Same box only                | No                           | No                       |
| Happy Coder                    | Vendor "Happy Server" (E2E enc) | Your CLI, through their tunnel | Yes (E2E, but still their infra) | No |
| GitHub Copilot `/remote on`    | github.com         | GitHub-hosted or your runner | Yes                          | No                       |
| Claude Managed Agents          | Cloudflare         | Cloudflare microVMs          | Yes                          | No                       |
| NetClode                       | Vendor managed     | Vendor microVMs              | Yes                          | No                       |
| claude-code-webui / similar    | Your box           | Same box only                | No                           | No                       |
| **Agents Anywhere (this repo)**| **Your box**       | **N machines you own, anywhere** | **No**                  | **Yes**                  |

If "my source code must not leave VPC-A, but I still want to drive an agent
on my phone from a cafe" is a problem you actually have, this is the only
OSS project I am aware of that solves it end-to-end.

---

## Deployment topologies

The same binary supports four very different setups. Pick the one that
matches what you have.

### 1. Laptop only (dev)

```
   Browser ────▶ Hub ────▶ worker-claude     (all on one laptop, docker compose)
                       ────▶ worker-copilot
```

Single `docker compose up`. No tunnel, no Cloudflare, no auth (dev mode).
Use this to evaluate the project in 5 minutes.

### 2. Laptop + home server (homelab)

```
   Phone ─▶ CF Tunnel ─▶ Hub (laptop)
                            ├──▶ worker-claude   (laptop)
                            └──▶ worker-claude   (home server, 192.168.1.25:8002)
```

Workers registered in `workers.json` with their LAN addresses. Hub re-reads
the file on every WS connect, so adding a worker is one line + nothing to
restart. From your phone you switch between machines in the sidebar.

### 3. Hub on VPS + workers in VPC (small team)

```
   Phone ─▶ CF Access ─▶ Hub (VPS or small EC2)
                            ├──▶ worker-claude  (EC2 in VPC, behind SG)
                            └──▶ worker-copilot (EC2 in VPC, behind SG)
```

Hub is the only public surface and is gated by Cloudflare Access (JWT
verified on WS handshake, key rotation handled automatically). Workers
listen on private subnets, only the hub can reach them, only their
shared `WORKER_SECRET` lets the hub in. Code never leaves the VPC.

### 4. Cross-account / regulated (enterprise, air-gap optional)

```
   Workstation ─▶ Hub (VPC-A, behind internal ALB + corporate SSO)
                     ├──▶ worker-claude   (VPC-B, dedicated to client A)
                     ├──▶ worker-claude   (VPC-C, dedicated to client B)
                     └──▶ worker-claude   (on-prem GPU box, intranet only)
```

Sessions are segregated by worker. Each worker only sees the project roots
mounted into its container. Anthropic / GitHub API egress is the only
outbound traffic, and you can lock it to specific endpoints with a security
group or HTTP egress proxy. Hub and all workers can run entirely inside
your network with no internet exposure.

---

## Architecture

Hub + multi-agent workers running in Docker.

- **Hub** (`hub/`): FastAPI. Frontend static serve, Cloudflare Access auth,
  WebSocket router between browser and N workers, push notifications,
  per-worker model registry.
- **Workers** (`worker-claude/`, `worker-copilot/`): FastAPI. One per agent
  SDK family. Each handles project scanning, terminal, file browser,
  session management, and routes prompts into its SDK's session loop.
  - `worker-claude` - Anthropic `claude-agent-sdk`
  - `worker-copilot` - `github-copilot-sdk` (GitHub Copilot CLI)
- **Shared** (`shared/worker_shared/`): SDK-agnostic modules (db, files,
  terminal, locks, projects service, session registry) consumed by every
  worker via a uv path source. Adding a new agent SDK = one new worker
  directory reusing these modules.
- **Frontend** (`frontend/`): React + Vite + Tailwind, builds to
  `frontend-dist/` which is volume-mounted into hub (no hub rebuild on
  frontend changes).
- **Tunnel** (optional): cloudflared, included in `compose.yml`.
- **Auth** (optional): Cloudflare Access JWT verified on WS handshake.
  Dev bypass if unconfigured.

```
   Browser ─WS─▶ Hub ─WS─▶ worker-claude    (Claude SDK)
                       ─WS─▶ worker-copilot  (Copilot SDK)
                       ─WS─▶ worker-...      (any future SDK)
```

Workers are configured in `workers.json` (id, type, label, url, secret).
Hub re-reads `workers.json` on every frontend WS connect (no hub restart
needed to add a worker) and keeps retrying any worker that isn't reachable
yet via a per-WS retry loop (default 30s). When a worker (re)appears, hub
pushes a fresh `projects` payload to the frontend automatically.

Each project carries a `worker_id` + `worker_type`; routing is by project
ID so prompts go to the worker that owns the conversation. Active session
is the worker of the most recent `new_session` / `resume_session`.

---

## Quick start

```bash
# Clone, configure
git clone <this repo>
cd claude_cloud
cp .env.example .env             # fill in UID, GID, WORKER_SECRET

# Configure workers (id, type, label, url, secret)
cp workers.json1 workers.json    # or edit by hand

# Build + start (hub, both workers, optional cloudflared)
docker compose up -d --build

# Open http://localhost:8001 (or your tunnel hostname)
```

Adding a remote worker from another machine:

```bash
# On the remote machine: bring up a standalone worker
docker compose -f worker-claude-compose.yml up -d --build

# On the hub machine: append a new entry to workers.json
#   { "id": "big-box", "type": "claude", "label": "Big Box",
#     "url": "ws://10.0.0.42:8002/ws", "secret": "..." }

# No hub restart needed - hub re-reads on next browser WS connect.
```

Frontend dev:

```bash
cd frontend && npm install && npm run build
# Hub serves from frontend-dist/ volume mount - no rebuild needed
# Hub Python changes do require a rebuild:
docker compose up -d --build hub
```

---

## Compliance / security posture

- **Data path:** browser - hub - worker. Nothing in between except the
  optional Cloudflare Tunnel (you control the tunnel; CF is the proxy, not
  the agent host).
- **Where the source code is read:** only on the worker container, from a
  bind-mounted host directory. Worker -> hub WS carries diffs and tool
  output but no checkouts.
- **Outbound calls:** worker -> Anthropic API (`api.anthropic.com:443`) for
  `worker-claude`, worker -> GitHub Copilot API for `worker-copilot`.
  Lockable behind a security group / egress proxy if needed. Hub has no
  outbound dependencies once running.
- **Auth between browser and hub:** Cloudflare Access JWT (HS256 from CF's
  rotating JWKS, validated against `CF_ACCESS_AUD` and an email
  allowlist). Fail-closed in `CLAUDE_WEB_ENV=prod` if the CF env vars are
  missing.
- **Auth between hub and worker:** shared secret (`WORKER_SECRET`) over
  the WS handshake. Worker -> hub push callbacks (HTTP POST
  `/api/internal/push`) carry the same secret in `X-Worker-Secret`.
- **Secrets at rest:** `~/.claude-web/push_subs.json` (web push
  subscriptions, atomic write), SQLite DB (project index, no source code).
  No secrets logged.
- **Per-session locks:** a session is bound to the WS connection that
  resumed it. A second device gets `session_locked` with the current
  holder's device label and can either back off or take over (sends
  `lock_revoked` to the previous owner, who is flipped to read-only).
- **Permission broker:** tool calls surface as `permission_request` blocks
  for explicit Allow / Deny unless `auto_approve` is set per project or
  armed per prompt. User's `~/.claude/settings.json` `permissions.allow`
  patterns still apply upstream.

---

## Features

### Chat & streaming
- Real-time WS streaming (text deltas, thinking blocks, tool calls)
- Markdown + syntax highlight (ReactMarkdown + rehype-highlight)
- LCS diff viewer for Edit / Write tool calls (green/red lines,
  truncation at 200 lines)
- Activity bar (Thinking / Running tool / Sending) above the composer
- Tool blocks collapsed by default
- Smart auto-scroll (only when user is near the bottom)
- Session history search with match counter and prev/next navigation

### Composer
- Slash command autocomplete (Up/Down/Tab/Enter/Escape):
  - `/clear`, `/compact`, `/new`
  - `/plan`, `/act` - frontend-only plan mode (prepends a read-only
    instruction to the prompt)
  - `/model <id>` - mid-session model switch. Model list is fetched per
    worker (Copilot via `CopilotClient.list_models()`, Claude from a
    hub-side default that you can override). `/model default` clears the
    per-session override.
- Image upload: file picker, clipboard paste, drag-and-drop
  (PNG/JPEG/GIF/WEBP), with thumbnail preview strip
- Voice input via Web Speech API
- Per-prompt auto-approve checkbox
- Stream tokens toggle (persisted to localStorage)
- Mobile-aware: Enter sends on desktop, overflow menu on touch

### Projects & sessions
- Sidebar with collapsible project list, lazy-loaded sessions
- Project scanner walks `~/.claude/projects/` (or
  `~/.copilot/session-state/`) on worker startup, registers in SQLite
- Projects sorted by last session modification time
- Project search/filter (name or path, case-insensitive)
- Worker filter dropdown (shown when more than one worker is connected)
- New project browser - modal FS navigator with breadcrumbs, "New folder"
- Projects whose `cwd` doesn't exist on the worker shown greyed-out
- Active project/session persisted to localStorage (survives reload / push tap)
- Paginated session history (`before_uuid` / `has_more`)

### AskUserQuestion (multi-question)
- Worker sends full list of questions from a single AskUserQuestion call
- One input per question with option buttons that pre-fill the field
- Minimize/expand toggle, internal scroll, dismiss button to unblock the
  agent with empty answers

### File browser
- Sandboxed directory listing (file/folder icons, sizes, symlink-aware)
- File viewer: text files with syntax highlight, image preview, binary
  detection, "too large" notice
- File editing: textarea, Save/Cancel, Ctrl/Cmd+S shortcut
- `write_file` WS message persists back to disk

### Terminal
- xterm.js + FitAddon, JetBrains Mono / Fira Code / Cascadia Code stack
- PTY fork on worker side, asyncio reader
- Dynamic resize via ResizeObserver
- Hide without kill - PTY stays alive when terminal is closed

### Permissions
- Per-project `auto_approve` flag (sidebar dot + header pill, toggleable
  mid-session)
- Per-prompt one-shot override via composer checkbox (armed before query,
  disarmed after `result`)
- Otherwise every tool call surfaces a `permission_request` block above
  the composer

### Locks
- Lock is per `session_id`, not per project. Held by the WS connection
  that most recently called `new_session` / `resume_session`.
- Second client resuming the same session gets `session_locked` with the
  current holder's device label. Modal offers "Take over" (`force: true`).
- Takeover sends `lock_revoked` to the previous owner; chat store flips
  to read-only.
- Locks auto-release on WS disconnect / timeout.

### Notifications (dedup'd)
- **Web Push** (server-side): worker POSTs `push_notify` to
  `hub/api/internal/push` (authenticated with `X-Worker-Secret`). This
  out-of-band path survives client WS disconnects (PWA swiped away on
  Android, etc.). Subscriptions persisted to `~/.claude-web/push_subs.json`
  (atomic write); dead endpoints (403/404/410) pruned automatically.
- **Local** (client-side): `useVisibilityNotify` fallback when push is
  not subscribed. Two-tone Web Audio chime (880Hz / 1100Hz).

Both use `tag: "claude-result"` so they don't stack. Local notify is
skipped when push is active. Service worker suppresses push banner when
any client window has `visibilityState === 'visible'`.

### PWA
- Web App Manifest with icons (192px + 512px, separate `any` and `maskable`)
- Service worker (install/activate/fetch/notification click handlers)
- Screen Wake Lock during active streaming (re-acquired on visibility change)
- Cloudflare Access bypass rules for public PWA assets
  (`/manifest.json`, `/icon-*.png`, `/sw.js`) so Chrome can generate a
  proper WebAPK

---

## Conventions

- **Python**: async everywhere, full type hints, `mypy --strict`,
  `structlog` for logging
- **TypeScript**: strict mode, functional components + hooks, Tailwind
  utility-first
- **WS protocol**: every message is `{ type, payload }` JSON
- **Commits**: conventional commits (`feat:`, `fix:`, `refactor:`)
- **Dependency posture**: no socket.io, no Redux, no react-query - keep
  the surface minimal

---

## Module layout

```
hub/src/
├── main.py              FastAPI app, static serving, WS endpoint, auth, internal push API
├── auth/cf_access.py    CF Access JWT verification + key rotation (1h cert cache)
├── push/manager.py      Web Push (VAPID) - persistent subs, dead endpoint pruning
├── workers/
│   ├── registry.py      Read workers.json - WorkerInfo list
│   ├── connection.py    WorkerConnection - WS client to a single worker
│   └── project_index.py ProjectIndex - hub_id <-> (worker_id, worker_pid) mapping
└── ws/handler.py        Multi-worker WS proxy + message routing

shared/worker_shared/    (path-installed in every worker via uv sources)
├── db.py                aiosqlite singleton + schema (projects table)
├── files/browser.py     Sandboxed file browser (symlink-aware) + write_file
├── terminal/session.py  PTY terminal session (pty fork + asyncio reader)
├── locks/manager.py     per-session lock registry (takeover supported)
├── projects/service.py  project CRUD (list, get, set_auto_approve)
└── sdk/registry.py      Session parking registry (keeps sessions alive after
                         WS disconnect) - typed against a structural Protocol

worker-claude/src/       (Anthropic Claude SDK runtime)
├── main.py              FastAPI app, WS endpoint with shared secret auth
├── projects/scanner.py  walk ~/.claude/projects/, register in DB
├── sessions/reader.py   parse .jsonl, list sessions, paginated history
├── sdk/
│   ├── session.py       ClaudeSDKClient wrapper (streaming, model switch, push)
│   ├── manager.py       SessionManager - owns active session per WS
│   ├── permissions.py   PermissionBroker - gated tool-use approvals
│   └── registry.py      Session parking after WS disconnect
└── ws/handler.py        WS routing + heartbeat

worker-copilot/src/      (GitHub Copilot SDK runtime - mirrors worker-claude)
├── main.py              FastAPI app, lifespan starts/stops CopilotClient
├── projects/scanner.py  walk ~/.copilot/session-state/, group by workspace_path
├── sessions/reader.py   parse events.jsonl (Copilot raw camelCase), build
│                        ChatMessage[] with tool blocks attached by toolCallId
├── sdk/
│   ├── client.py        Singleton CopilotClient (bundled CLI subprocess)
│   ├── session.py       CopilotSession wrapper - event mapping
│   │                    (AssistantMessageDelta -> text_delta, ToolExecutionStart
│   │                    -> tool_call, SessionIdle -> result, etc.)
│   ├── manager.py       SessionManager - owns active session per WS
│   └── permissions.py   PermissionBroker - bridges to SDK's
│                        on_permission_request callback
└── ws/handler.py        WS routing + heartbeat + list_models endpoint

frontend/src/
├── App.tsx              Main app shell, WS message dispatch, search
├── stores/
│   ├── chat.ts          Zustand - messages, streaming state, plan mode, model
│   ├── projects.ts      Zustand - project list, sessions, active selection
│   └── files.ts         Zustand - file browser state, directory cache
├── hooks/
│   ├── useWebSocket.ts          Auto-reconnect WS with heartbeat
│   ├── usePushNotifications.ts  VAPID push subscription management
│   ├── useVisibilityNotify.ts   Client-side notification fallback
│   ├── useWakeLock.ts           Screen wake lock during streaming
│   └── useSpeechInput.ts        Web Speech API voice dictation
├── components/
│   ├── Sidebar.tsx             Projects, sessions, search, worker filter, logout
│   ├── Composer.tsx            Input, slash commands, images, voice, auto-approve
│   ├── Message.tsx             Markdown + syntax highlight + search highlight
│   ├── ToolBlock.tsx           Collapsible tool call display + DiffView
│   ├── DiffView.tsx            LCS line diff (add/del/ctx, green/red)
│   ├── FileBrowser.tsx         Directory listing + file viewer/editor
│   ├── Terminal.tsx            xterm.js PTY terminal
│   ├── StreamingStatus.tsx     Activity bar (thinking/tool/waiting)
│   ├── UserInputPrompt.tsx     Multi-question panel with minimize
│   ├── NewProjectBrowser.tsx   Modal FS browser for new projects
│   └── PermissionPrompt.tsx    Inline Allow/Deny above composer
├── commands.ts             Slash command definitions + matchCommands()
└── public/
    └── sw.js               Service worker (push, fetch, notification click)
```

---

## Docker

```
docker/
├── hub.Dockerfile             Multi-stage: node (frontend) - uv (deps) - python:3.13-slim
├── worker-claude.Dockerfile   Multi-stage: uv (deps) - python:3.13-slim + Node.js + Claude CLI
├── worker-copilot.Dockerfile  Multi-stage: uv (deps) - python:3.13-slim
│                              (github-copilot-sdk wheel bundles the Copilot CLI
│                              binary so no separate Node install)
└── entrypoint.sh              Injects user into /etc/passwd (fixes "I have no name!")
```

- `compose.yml` - hub + local worker-claude + local worker-copilot + cloudflared
- `worker-claude-compose.yml` - standalone worker-claude for a remote machine
- `worker-copilot-compose.yml` - standalone worker-copilot for a remote machine
- `workers.json` - worker registry; one entry per worker:
  ```json
  {
    "id": "big-worker",
    "type": "claude",
    "label": "Big Worker",
    "url": "ws://192.168.1.25:8002/ws",
    "secret": "changeme"
  }
  ```
  `type` is free-form (`claude`, `copilot`, ...). Frontend renders it as a
  per-project badge and uses it to pick the right model list for `/model`.

Volume mounts use the same absolute paths as the host so that Claude /
Copilot session history (`~/.claude/projects/*.jsonl`,
`~/.copilot/session-state/`) resolves correctly inside containers.

Frontend build output (`frontend-dist/`) is volume-mounted into hub at
`/app/src/static-mount`. Hub prefers this over the baked-in copy, so
`npm run build` updates the UI without rebuilding the hub image.

---

## WebSocket protocol

**Client - Hub - Worker:**

- `ping`, `prompt {text, images?, auto_approve?, stream?}`, `interrupt`
- `list_projects`, `list_sessions {project_id}`,
  `session_history {project_id, session_id, limit?, before_uuid?}`
- `new_session {project_id, model?}`,
  `resume_session {project_id, session_id, force?, model?}`
- `set_auto_approve {project_id, auto_approve}`
- `set_model {model}`, `set_plan_mode {plan_mode}`
- `list_models` (hub queries each worker on connect; worker-copilot returns
  `CopilotClient.list_models()`, worker-claude falls back to a hub-side
  default - see hub `_DEFAULT_MODELS_BY_TYPE`)
- `approve_tool {tool_use_id}`, `deny_tool {tool_use_id, reason?}`
- `user_input_response {tool_use_id, answers: string[]}` (one per question;
  legacy `{tool_use_id, answer}` still accepted)
- `list_directory {project_id, path?}`, `read_file {project_id, path}`,
  `write_file {project_id, path, content}`
- `browse_fs {path?, worker_id?}`, `create_directory {path, worker_id?}`,
  `create_project {path, worker_id?}`
- `terminal_open {project_id, cols?, rows?}`, `terminal_input {data}`,
  `terminal_resize {cols, rows}`, `terminal_close`

**Worker - Hub - Client:**

- `pong`, `text_delta`, `thinking`, `tool_call`, `tool_result`, `result`,
  `system`, `error`
- `projects` payload carries `{projects, workers}` where each worker has
  `{id, label, type, connected, models: [{id, name}]}` so the sidebar can
  render the worker filter and `/model` autocomplete without extra
  round-trips
- `sessions`, `session_history` (with `has_more` / `oldest_uuid`)
- `session_started` (with `cwd`, `session_id`, `resumed`, `auto_approve`,
  `is_busy`)
- `session_locked`, `lock_revoked`
- `permission_request`, `project_updated`, `project_created`
- `directory`, `file_content`, `file_written`, `fs_directory`
- `terminal_ready`, `terminal_output {data}`, `terminal_closed`
- `user_input_request {tool_use_id, questions: [{question, options}]}`
- `push_notify {title, body}` (intercepted by hub - Web Push, or
  out-of-band via HTTP)
- `model_changed {model}` (system subtype, shown as info banner in chat)
- `task_event` (Monitor/TaskCreate tick events parsed from XML in
  UserMessage)
- `models` (worker-copilot's reply to `list_models`; hub-internal)

Hub remaps project IDs:
`hub_id = worker_index * 1_000_000 + worker_project_id`

---

## Environment variables

| Variable                       | Purpose                                                   |
| ------------------------------ | --------------------------------------------------------- |
| `UID` / `GID`                  | Container user mapping (match host user)                  |
| `PORT`                         | Host bind for hub (default `127.0.0.1:8001`)              |
| `CLAUDE_WEB_ENV`               | `prod` = fail-closed auth, `dev` = bypass when CF unset   |
| `CF_ACCESS_TEAM_DOMAIN`        | e.g. `myteam.cloudflareaccess.com`                        |
| `CF_ACCESS_AUD`                | Application Audience tag from CF dashboard                |
| `CF_ACCESS_ALLOWED_EMAILS`     | Comma-separated email allowlist                           |
| `WORKER_SECRET`                | Shared secret for hub <-> worker auth                     |
| `WORKERS_CONFIG`               | Path to `workers.json` (default `/config/workers.json`)   |
| `CLAUDE_WEB_DB_PATH`           | SQLite path (default `~/.claude-web/db.sqlite`)           |
| `HUB_URL`                      | Hub URL for worker out-of-band push (e.g. `http://hub:8001`) |
| `TUNNEL_TOKEN`                 | Cloudflare Tunnel token (if running cloudflared service)  |

Without the `CF_ACCESS_*` vars, auth is bypassed (dev mode).

---

## Host SSH access (from inside the worker container)

The worker container has SSH access back to the host. Useful for anything
that can't be done from inside the sandbox: `docker compose` rebuilds,
poking running containers, inspecting host network state, hitting
services bound to host-only ports, etc.

---

## Testing

```bash
cd worker-claude && uv run pytest
cd worker-copilot && uv run pytest
cd hub && uv run pytest
```

---

## License

[GNU Affero General Public License v3.0](LICENSE).

If you want to embed this in a commercial product, open an issue and let's
talk.
