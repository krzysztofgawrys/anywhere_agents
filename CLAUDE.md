# Claude Web

Web interface for local Claude Code. Mobile + desktop, accessed via Cloudflare Tunnel.

## Architecture

Hub + Workers architecture running in Docker:

- **Hub**: FastAPI — frontend proxy, Cloudflare Access auth, WebSocket router to workers, push notifications
- **Workers**: FastAPI — agent SDK runtime, project scanning, terminal, file browser, session management.
  Each worker is specialized for one agent SDK; currently `worker-claude` (Claude SDK). Future: `worker-copilot`, etc.
- **Frontend**: React + Vite + Tailwind, builds to `frontend-dist/`
- **Tunnel**: cloudflared (external)
- **Auth**: Cloudflare Access JWT verified on WS handshake (dev bypass if unconfigured)

```
┌─────────────┐     ┌─────────────┐
│   Browser   │────▶│     Hub     │──┬──▶ worker-claude (local/Docker)
│  (PWA/Web)  │ WS  │  (Docker)   │  ├──▶ worker-claude (big-worker/remote)
└─────────────┘     └─────────────┘  └──▶ worker-claude (small-worker/remote)
```

Workers are configured in `workers.json`. Hub connects to all workers on
frontend WS connect, fans out project lists, and routes messages to the
correct worker based on project ID.

## Quick Start

```bash
# Docker (production)
cp .env.example .env   # fill in UID, GID, WORKER_SECRET
docker compose up -d --build

# Frontend dev (from worker-claude or host)
cd frontend && npm install && npm run build
# Hub serves from frontend-dist/ volume mount — no rebuild needed

# Hub changes require image rebuild:
docker compose up -d --build hub
```

## Conventions

- Python: async everywhere, full type hints, `mypy --strict`, `structlog` for logging
- TypeScript: strict mode, functional components + hooks, Tailwind utility-first
- WS protocol: all messages are `{ type, payload }` JSON
- Commits: conventional commits (`feat:`, `fix:`, `refactor:`)
- No socket.io, no Redux, no react-query — keep deps minimal

## Module Layout

```
hub/src/
├── main.py              FastAPI app, static serving, WS endpoint, auth
├── auth/cf_access.py    CF Access JWT verification + key rotation
├── push/manager.py      Web Push (VAPID) notification manager
├── workers/
│   ├── registry.py      Read workers.json → WorkerInfo list
│   ├── connection.py    WorkerConnection — WS client to a single worker
│   └── project_index.py ProjectIndex — hub_id ↔ (worker_id, worker_pid) mapping
└── ws/handler.py        Multi-worker WS proxy + message routing

worker-claude/src/         (one per agent SDK — see future worker-copilot/, etc.)
├── main.py              FastAPI app, WS endpoint with shared secret auth
├── db.py                aiosqlite singleton + schema
├── projects/
│   ├── scanner.py       walk ~/.claude/projects/, register in DB
│   └── service.py       project CRUD (list, get, set_auto_approve)
├── sessions/reader.py   parse .jsonl, list sessions, paginated history
├── files/browser.py     Sandboxed file browser (symlink-aware)
├── terminal/session.py  PTY terminal session (pty fork + asyncio reader)
├── sdk/
│   ├── session.py       ClaudeSDKClient wrapper (one conversation)
│   ├── manager.py       SessionManager — owns active session per WS
│   └── permissions.py   PermissionBroker — gated tool-use approvals
├── locks/manager.py     per-session lock registry (takeover supported)
└── ws/handler.py        WS routing + heartbeat

frontend/src/
├── App.tsx              Main app shell, WS message dispatch
├── stores/              Zustand stores (chat, projects, files)
├── hooks/               usePushNotifications, useVisibilityNotify, useWebSocket
└── components/          Sidebar, Composer, Message, FileBrowser, Terminal, etc.
```

## Docker

```
docker/
├── hub.Dockerfile           Multi-stage: node (frontend) → uv (deps) → python:3.13-slim
├── worker-claude.Dockerfile Multi-stage: uv (deps) → python:3.13-slim + Node.js + Claude CLI
└── entrypoint.sh            Injects user into /etc/passwd (fixes "I have no name!")
```

- `compose.yml` — hub + local worker-claude (laptop)
- `worker-claude-compose.yml` — standalone worker-claude for remote machines
- `workers.json` — worker registry (id, label, url, secret)

Volume mounts use same absolute paths as host so Claude session history
(~/.claude/projects/*.jsonl) resolves correctly inside containers.

Frontend build output (`frontend-dist/`) is volume-mounted into hub at
`/app/src/static-mount`. Hub prefers this over the baked-in copy, so
`npm run build` from worker-claude updates the UI without rebuilding the hub image.

## WebSocket Protocol

**Client → Hub → Worker:**
- `ping`, `prompt {text, images?, auto_approve?, stream?}`, `interrupt`
- `list_projects`, `list_sessions {project_id}`, `session_history {project_id, session_id, limit?, before_uuid?}`
- `new_session {project_id}`, `resume_session {project_id, session_id, force?}`
- `set_auto_approve {project_id, auto_approve}`
- `approve_tool {tool_use_id}`, `deny_tool {tool_use_id, reason?}`
- `user_input_response {tool_use_id, answers: string[]}` (one answer per question; legacy `{tool_use_id, answer}` still accepted)
- `list_directory {project_id, path?}`, `read_file {project_id, path}`
- `browse_fs {path?, worker_id?}`, `create_directory {path, worker_id?}`, `create_project {path, worker_id?}`
- `terminal_open {project_id, cols?, rows?}`, `terminal_input {data}`, `terminal_resize {cols, rows}`, `terminal_close`

**Worker → Hub → Client:**
- `pong`, `text_delta`, `thinking`, `tool_call`, `tool_result`, `result`, `system`, `error`
- `projects`, `sessions`, `session_history` (with has_more/oldest_uuid)
- `session_started` (with cwd, session_id, resumed, auto_approve), `session_locked`, `lock_revoked`
- `permission_request`, `project_updated`, `project_created`
- `directory`, `file_content`, `fs_directory`
- `terminal_ready`, `terminal_output {data}`, `terminal_closed`
- `user_input_request {tool_use_id, questions: [{question, options}]}` (AskUserQuestion may pose multiple)
- `push_notify {title, body}` (intercepted by hub → Web Push)

Hub remaps project IDs: `hub_id = worker_index * 1_000_000 + worker_project_id`

## Environment Variables

- `CF_ACCESS_TEAM_DOMAIN` — e.g. `myteam.cloudflareaccess.com`
- `CF_ACCESS_AUD` — Application Audience tag from CF dashboard
- `CF_ACCESS_ALLOWED_EMAILS` — comma-separated email allowlist
- `CLAUDE_WEB_ENV` — `prod` for fail-closed auth, `dev` (default) for bypass
- `WORKER_SECRET` — shared secret for hub↔worker authentication
- `WORKERS_CONFIG` — path to workers.json (default `/config/workers.json`)
- `CLAUDE_WEB_DB_PATH` — SQLite path (default `~/.claude-web/db.sqlite`)
- `UID` / `GID` — container user mapping (match host user)

Without CF vars, auth is bypassed (dev mode).

## Permission Model

- Per-project `auto_approve` flag (sidebar dot + header pill, toggleable while
  a session is active)
- Per-prompt one-shot override via composer checkbox ("Auto-approve tools for
  this prompt") — armed before query, disarmed after `result`
- When neither is on, every tool call surfaces a `permission_request` block
  above the composer for explicit Allow / Deny
- User's `~/.claude/settings.json` `permissions.allow` patterns still apply
  upstream of `can_use_tool` (loaded via `setting_sources=['user','project']`)

## Locks

- Lock is per session_id, not per project. Held by the WS connection that
  most recently called new_session/resume_session.
- Second client resuming the same session gets `session_locked` with the
  current holder's device label → modal offers "Take over" (`force: true`).
- Takeover sends `lock_revoked` to the previous owner; the chat store flips
  to read-only.
- Locks auto-release on WS disconnect / timeout.

## Notifications

Two systems, deduplicated:
- **Web Push** (server-side): worker sends `push_notify` → hub → VAPID push → SW `push` event
- **Local** (client-side): `useVisibilityNotify` — fallback when push subscription is not active

Both use `tag: "claude-result"` to avoid stacking. Local notify is skipped
when Web Push is active.

## Testing

```bash
cd worker-claude && uv run pytest
```
