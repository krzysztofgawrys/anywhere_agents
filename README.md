# Claude Web

Web interface for local agent CLIs (Claude Code, GitHub Copilot CLI). Mobile +
desktop, accessed via Cloudflare Tunnel. Installable as a PWA on Android/iOS.

## Architecture

Hub + multi-agent workers architecture running in Docker:

- **Hub**: FastAPI - frontend proxy, Cloudflare Access auth, WebSocket router to
  workers, push notifications, model registry per worker.
- **Workers**: FastAPI - one per agent SDK family. Each worker handles project
  scanning, terminal, file browser, session management, and routes prompts into
  its SDK's session loop. Shipped today:
  - `worker-claude` - Anthropic `claude-agent-sdk`
  - `worker-copilot` - `github-copilot-sdk` (GitHub Copilot CLI)
- **Shared**: `shared/worker_shared/` - SDK-agnostic modules (db, files,
  terminal, locks, projects service, session registry) consumed by every
  worker via a uv path source.
- **Frontend**: React + Vite + Tailwind, builds to `frontend-dist/`.
- **Tunnel**: cloudflared (external).
- **Auth**: Cloudflare Access JWT verified on WS handshake (dev bypass if
  unconfigured).

```
┌─────────────┐     ┌─────────────┐  ┌──▶ worker-claude   (Claude SDK)
│   Browser   │────▶│     Hub     │──┤
│  (PWA/Web)  │ WS  │  (Docker)   │  └──▶ worker-copilot  (Copilot SDK)
└─────────────┘     └─────────────┘
```

Workers are configured in `workers.json` (id, type, label, url, secret). Hub
re-reads `workers.json` on every frontend WS connect (no hub restart needed
to add a worker) and keeps trying to connect to any worker that isn't reachable
yet via a per-WS retry loop (default 30s). When a worker (re)appears, hub
pushes a fresh `projects` payload to the frontend automatically.

Each project carries a `worker_id` + `worker_type`; routing is by project ID
so prompts go to the worker that owns the conversation. Active session is
the worker of the most recent `new_session` / `resume_session`.

## Quick Start

```bash
# Docker (production)
cp .env.example .env   # fill in UID, GID, WORKER_SECRET
docker compose up -d --build

# Frontend dev (from worker-claude or host)
cd frontend && npm install && npm run build
# Hub serves from frontend-dist/ volume mount - no rebuild needed

# Hub changes require image rebuild:
docker compose up -d --build hub
```

## Conventions

- Python: async everywhere, full type hints, `mypy --strict`, `structlog` for logging
- TypeScript: strict mode, functional components + hooks, Tailwind utility-first
- WS protocol: all messages are `{ type, payload }` JSON
- Commits: conventional commits (`feat:`, `fix:`, `refactor:`)
- No socket.io, no Redux, no react-query - keep deps minimal

## Features

### Chat & Streaming

- Real-time streaming via WebSocket (text deltas, thinking blocks, tool calls)
- Markdown rendering (ReactMarkdown + remark-gfm) with syntax highlighting (rehype-highlight)
- Diff viewer for Edit/Write tool calls (LCS-based, green/red line highlighting, truncation at 200 lines)
- Streaming status bar above composer showing current activity (Thinking / Running tool / Sending)
- Tool call blocks collapsed by default (outputs can be huge)
- Smart auto-scroll - only scrolls to bottom when user is already near the bottom
- Session history search with match counter and prev/next navigation (Ctrl+F style)

### Composer

- Slash command autocomplete on `/` with keyboard navigation (Up/Down/Tab/Enter/Escape):
  - `/clear` - reset chat messages
  - `/compact` - summarize conversation to save context
  - `/new` - start a new session in current project
  - `/plan`, `/act` - toggle plan mode (prepends read-only instruction to prompt)
  - `/model <id>` - switch model mid-session; the autocomplete list is
    populated per active worker. Claude entries come from a hub-side default
    (`claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`);
    Copilot entries are pulled live from `CopilotClient.list_models()`
    (`auto`, `gpt-5.3-codex`, `gpt-4.1`, plus anything new GitHub ships).
    `/model default` always available - clears the per-session override.
- Image upload via file picker, clipboard paste, or drag-and-drop (PNG/JPEG/GIF/WEBP)
  with thumbnail preview strip
- Voice input via Web Speech API (mic button in overflow menu)
- Per-prompt auto-approve checkbox ("Auto-approve tools for this prompt")
- Stream tokens toggle (persisted to localStorage)
- Mobile-aware: Enter sends on desktop, overflow menu replaces inline buttons on touch

### Plan Mode & Model Picker

- Plan mode is frontend-only: `/plan` prepends a read-only instruction to the prompt,
  `/act` disables it. Header shows current mode (act/plan) as compact amber text.
- Model switching sends `set_model` WS message to worker, which calls the SDK's
  `client.set_model()` mid-session (no restart). Model change appears as an info
  banner in chat history.

### Projects & Sessions

- Sidebar with collapsible project list, lazy-loaded sessions per project
- Project scanner walks `~/.claude/projects/` on startup, registers in SQLite
- Projects sorted by last session modification time
- Project search/filter (case-insensitive by name or path)
- Worker filter dropdown (shown only when multiple workers connected)
- New project browser - modal FS navigator with breadcrumbs, "New folder" creation
- Projects whose `cwd` doesn't exist on the worker are shown greyed-out (unavailable)
- Active project/session persisted to localStorage (survives page reload, push tap)
- `new_session` / `resume_session` with proper cwd handling
- Paginated session history (`before_uuid` / `has_more`)

### AskUserQuestion (multi-question)

- Worker sends full list of questions from a single AskUserQuestion call
- Frontend renders one input per question with option buttons that pre-fill the field
- Minimize/expand toggle - collapses to a thin bar with "X/N answered" progress
- Panel has internal scroll (`max-h-60vh`), composer hidden while panel is expanded
- Dismiss button sends empty answers to unblock the agent

### File Browser

- Sandboxed directory listing with file/folder icons, sizes, symlink awareness
- File viewer: text files as syntax-highlighted code, image preview (PNG/JPEG/GIF/WEBP/SVG),
  binary detection, "too large" notice
- File editing: toggle to textarea, Save/Cancel buttons, Ctrl/Cmd+S shortcut
- `write_file` WS message for saving changes back to disk

### Terminal

- xterm.js with FitAddon, JetBrains Mono / Fira Code / Cascadia Code font stack
- PTY fork on worker side with asyncio reader
- Dynamic resize via ResizeObserver sending `terminal_resize` WS messages
- Hide without kill - X button hides the terminal, PTY stays alive on backend

### Permission Model

- Per-project `auto_approve` flag (sidebar dot + header pill, toggleable while
  a session is active)
- Per-prompt one-shot override via composer checkbox - armed before query,
  disarmed after `result`
- When neither is on, every tool call surfaces a `permission_request` block
  above the composer for explicit Allow / Deny
- User's `~/.claude/settings.json` `permissions.allow` patterns still apply
  upstream of `can_use_tool` (loaded via `setting_sources=['user','project']`)

### Locks

- Lock is per session_id, not per project. Held by the WS connection that
  most recently called new_session/resume_session.
- Second client resuming the same session gets `session_locked` with the
  current holder's device label - modal offers "Take over" (`force: true`).
- Takeover sends `lock_revoked` to the previous owner; the chat store flips
  to read-only.
- Locks auto-release on WS disconnect / timeout.

### Notifications

Two systems, deduplicated:
- **Web Push** (server-side): worker sends `push_notify` via HTTP POST to
  `hub/api/internal/push` (authenticated with `X-Worker-Secret`). This
  out-of-band path survives client WS disconnects (e.g. PWA swiped away
  on Android). Subscriptions persisted to `~/.claude-web/push_subs.json`
  (atomic write), dead endpoints (403/404/410) pruned automatically.
- **Local** (client-side): `useVisibilityNotify` - fallback when push
  subscription is not active. Fires a two-tone Web Audio chime (880Hz/1100Hz).

Both use `tag: "claude-result"` to avoid stacking. Local notify is skipped
when Web Push is active. Service worker suppresses push banner when any
client window has `visibilityState === 'visible'`.

### PWA

- Web App Manifest with icons (192px + 512px, separate `any` and `maskable`)
- Service worker with install/activate/fetch handlers (minimal pass-through
  for proper PWA qualification)
- Screen Wake Lock during active streaming sessions (re-acquires on visibility change)
- Cloudflare Access bypass rules for public PWA assets (`/manifest.json`,
  `/icon-*.png`, `/sw.js`) so Chrome can generate a proper WebAPK

## Module Layout

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

shared/worker_shared/        (path-installed in every worker via uv sources)
├── db.py                aiosqlite singleton + schema (projects table)
├── files/browser.py     Sandboxed file browser (symlink-aware) + write_file
├── terminal/session.py  PTY terminal session (pty fork + asyncio reader)
├── locks/manager.py     per-session lock registry (takeover supported)
├── projects/service.py  project CRUD (list, get, set_auto_approve)
└── sdk/registry.py      Session parking registry (keeps sessions alive after
                         WS disconnect) - typed against a structural Protocol

worker-claude/src/           (Anthropic Claude SDK runtime)
├── main.py              FastAPI app, WS endpoint with shared secret auth
├── projects/scanner.py  walk ~/.claude/projects/, register in DB
├── sessions/reader.py   parse .jsonl, list sessions, paginated history
├── sdk/
│   ├── session.py       ClaudeSDKClient wrapper (streaming, model switch, push)
│   ├── manager.py       SessionManager - owns active session per WS
│   ├── permissions.py   PermissionBroker - gated tool-use approvals
│   └── registry.py      Session parking after WS disconnect
└── ws/handler.py        WS routing + heartbeat

worker-copilot/src/          (GitHub Copilot SDK runtime - mirrors worker-claude)
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
│   ├── useWebSocket.ts       Auto-reconnect WS with heartbeat
│   ├── usePushNotifications.ts  VAPID push subscription management
│   ├── useVisibilityNotify.ts   Client-side notification fallback
│   ├── useWakeLock.ts           Screen wake lock during streaming
│   └── useSpeechInput.ts       Web Speech API voice dictation
├── components/
│   ├── Sidebar.tsx          Projects, sessions, search, worker filter, logout
│   ├── Composer.tsx         Input, slash commands, images, voice, auto-approve
│   ├── Message.tsx          Markdown + syntax highlight + search highlight
│   ├── ToolBlock.tsx        Collapsible tool call display + DiffView
│   ├── DiffView.tsx         LCS line diff (add/del/ctx, green/red)
│   ├── FileBrowser.tsx      Directory listing + file viewer/editor
│   ├── Terminal.tsx         xterm.js PTY terminal
│   ├── StreamingStatus.tsx  Activity bar (thinking/tool/waiting)
│   ├── UserInputPrompt.tsx  Multi-question panel with minimize
│   ├── NewProjectBrowser.tsx  Modal FS browser for new projects
│   └── PermissionPrompt.tsx   Inline Allow/Deny above composer
├── commands.ts          Slash command definitions + matchCommands() (model
│                        autocomplete entries are injected dynamically per
│                        active worker, see App.tsx modelCommands useMemo)
├── utils/
│   └── image.ts         Image resize/compress/base64 utilities
└── public/
    └── sw.js            Service worker (push, fetch, notification click)
```

## Docker

```
docker/
├── hub.Dockerfile             Multi-stage: node (frontend) - uv (deps) - python:3.13-slim
├── worker-claude.Dockerfile   Multi-stage: uv (deps) - python:3.13-slim + Node.js + Claude CLI
├── worker-copilot.Dockerfile  Multi-stage: uv (deps) - python:3.13-slim
│                              (github-copilot-sdk wheel bundles the Copilot CLI binary
│                              so no separate Node install)
└── entrypoint.sh              Injects user into /etc/passwd (fixes "I have no name!")
```

- `compose.yml` - hub + local worker-claude + local worker-copilot (laptop)
- `worker-claude-compose.yml` - standalone worker-claude for remote machines
- `worker-copilot-compose.yml` - standalone worker-copilot for remote machines
- `workers.json` - worker registry (one entry per worker):
  ```json
  {"id": "local-copilot", "type": "copilot", "label": "Laptop",
   "url": "ws://worker-copilot:8003/ws", "secret": "changeme"}
  ```
  `type` is free-form (`claude`, `copilot`, ...); frontend renders it as a
  per-project badge and uses it to pick the right model list for `/model`.

Volume mounts use same absolute paths as host so Claude session history
(~/.claude/projects/*.jsonl) resolves correctly inside containers.

Frontend build output (`frontend-dist/`) is volume-mounted into hub at
`/app/src/static-mount`. Hub prefers this over the baked-in copy, so
`npm run build` from worker-claude updates the UI without rebuilding the hub image.

## WebSocket Protocol

**Client - Hub - Worker:**
- `ping`, `prompt {text, images?, auto_approve?, stream?}`, `interrupt`
- `list_projects`, `list_sessions {project_id}`, `session_history {project_id, session_id, limit?, before_uuid?}`
- `new_session {project_id, model?}`, `resume_session {project_id, session_id, force?, model?}`
- `set_auto_approve {project_id, auto_approve}`
- `set_model {model}`, `set_plan_mode {plan_mode}`
- `list_models` (hub queries each worker on connect; worker-copilot returns
  `CopilotClient.list_models()`, worker-claude doesn't implement so hub falls
  back to a hardcoded type-default - see hub `_DEFAULT_MODELS_BY_TYPE`)
- `approve_tool {tool_use_id}`, `deny_tool {tool_use_id, reason?}`
- `user_input_response {tool_use_id, answers: string[]}` (one answer per question; legacy `{tool_use_id, answer}` still accepted)
- `list_directory {project_id, path?}`, `read_file {project_id, path}`, `write_file {project_id, path, content}`
- `browse_fs {path?, worker_id?}`, `create_directory {path, worker_id?}`, `create_project {path, worker_id?}`
- `terminal_open {project_id, cols?, rows?}`, `terminal_input {data}`, `terminal_resize {cols, rows}`, `terminal_close`

**Worker - Hub - Client:**
- `pong`, `text_delta`, `thinking`, `tool_call`, `tool_result`, `result`, `system`, `error`
- `projects` payload now carries `{projects, workers}` where each worker has
  `{id, label, type, connected, models: [{id, name}]}` so the sidebar can
  render the worker filter and `/model` autocomplete without extra round-trips
- `sessions`, `session_history` (with has_more/oldest_uuid)
- `session_started` (with cwd, session_id, resumed, auto_approve, is_busy)
- `session_locked`, `lock_revoked`
- `permission_request`, `project_updated`, `project_created`
- `directory`, `file_content`, `file_written`, `fs_directory`
- `terminal_ready`, `terminal_output {data}`, `terminal_closed`
- `user_input_request {tool_use_id, questions: [{question, options}]}` (AskUserQuestion may pose multiple)
- `push_notify {title, body}` (intercepted by hub - Web Push, or out-of-band via HTTP)
- `model_changed {model}` (system subtype, shown as info banner in chat)
- `task_event` (Monitor/TaskCreate tick events parsed from XML in UserMessage)
- `models` (worker-copilot's reply to `list_models`; hub-internal)

Hub remaps project IDs: `hub_id = worker_index * 1_000_000 + worker_project_id`

## Environment Variables

- `CF_ACCESS_TEAM_DOMAIN` - e.g. `myteam.cloudflareaccess.com`
- `CF_ACCESS_AUD` - Application Audience tag from CF dashboard
- `CF_ACCESS_ALLOWED_EMAILS` - comma-separated email allowlist
- `CLAUDE_WEB_ENV` - `prod` for fail-closed auth, `dev` (default) for bypass
- `WORKER_SECRET` - shared secret for hub<->worker authentication
- `WORKERS_CONFIG` - path to workers.json (default `/config/workers.json`)
- `CLAUDE_WEB_DB_PATH` - SQLite path (default `~/.claude-web/db.sqlite`)
- `HUB_URL` - hub URL for worker's out-of-band push HTTP calls (e.g. `http://hub:8001`)
- `UID` / `GID` - container user mapping (match host user)

Without CF vars, auth is bypassed (dev mode).

## Host SSH Access (from inside the worker container)

The worker container has SSH access back to the WSL2 host laptop. Useful for
anything that can't be done from inside the sandbox: `docker compose` rebuilds,
poking running containers, inspecting host network state, hitting services
bound to host-only ports, etc.

## Testing

```bash
cd worker-claude && uv run pytest
```
