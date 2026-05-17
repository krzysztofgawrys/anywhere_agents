# Claude Web

Web interface for local Claude Code. Mobile + desktop, accessed via Cloudflare Tunnel.

## Quick Start

```bash
# Backend
cd backend && uv sync && uv run uvicorn src.main:app --reload --host 127.0.0.1 --port 8001

# Frontend
cd frontend && npm install && npm run dev

# Both (hot reload)
./scripts/dev.sh
```

## Architecture

- Backend: FastAPI (native, not containerized) — needs full dev environment
- Frontend: React + Vite + Tailwind, builds to `backend/src/static/`
- Tunnel: cloudflared (currently external; `docker/compose.yml` reserved)
- Auth: Cloudflare Access JWT verified on WS handshake (dev bypass if unconfigured)

## Conventions

- Python: async everywhere, full type hints, `mypy --strict`, `structlog` for logging
- TypeScript: strict mode, functional components + hooks, Tailwind utility-first
- WS protocol: all messages are `{ type, payload }` JSON
- Commits: conventional commits (`feat:`, `fix:`, `refactor:`)
- No socket.io, no Redux, no react-query — keep deps minimal

## Module Layout

```
backend/src/
├── main.py              FastAPI app, lifespan scans projects, WS endpoint
├── auth/cf_access.py    CF Access JWT verification
├── db.py                aiosqlite singleton + schema
├── projects/
│   ├── scanner.py       walk ~/.claude/projects/, register in DB
│   └── service.py       project CRUD (list, get, set_auto_approve)
├── sessions/reader.py   parse .jsonl, list sessions, paginated history
├── sdk/
│   ├── session.py       ClaudeSDKClient wrapper (one conversation)
│   ├── manager.py       SessionManager — owns active session per WS, locks
│   └── permissions.py   PermissionBroker — gated tool-use approvals
├── locks/manager.py     per-session lock registry (takeover supported)
└── ws/handler.py        WS routing + heartbeat
```

## WebSocket Protocol

**Client → Server:**
- `ping`, `prompt {text, auto_approve?}`, `interrupt`
- `list_projects`, `list_sessions {project_id}`, `session_history {project_id, session_id, limit?, before_uuid?}`
- `new_session {project_id}`, `resume_session {project_id, session_id, force?}`
- `set_auto_approve {project_id, auto_approve}`
- `approve_tool {tool_use_id}`, `deny_tool {tool_use_id, reason?}`

**Server → Client:**
- `pong`, `text_delta`, `thinking`, `tool_call`, `tool_result`, `result`, `system`, `error`
- `projects`, `sessions`, `session_history` (with has_more/oldest_uuid)
- `session_started` (with cwd, resumed, auto_approve), `session_locked`, `lock_revoked`
- `permission_request`, `project_updated`

## Environment Variables

- `CF_ACCESS_TEAM_DOMAIN` — e.g. `myteam.cloudflareaccess.com`
- `CF_ACCESS_AUD` — Application Audience tag from CF dashboard
- `CF_ACCESS_ALLOWED_EMAILS` — comma-separated email allowlist
- `CLAUDE_WEB_DB_PATH` — SQLite path (default `~/.claude-web/db.sqlite`)

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

## Testing

```bash
cd backend && uv run pytest    # 35 tests across ws, sessions, projects, locks, permissions
```
