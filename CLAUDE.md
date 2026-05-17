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
- Tunnel: cloudflared in Docker (`docker/compose.yml`)
- Auth: Cloudflare Access JWT verified on WS handshake

## Conventions

- Python: async everywhere, full type hints, `mypy --strict`, `structlog` for logging
- TypeScript: strict mode, functional components + hooks, Tailwind utility-first
- WS protocol: all messages are `{ type, payload }` JSON
- Commits: conventional commits (`feat:`, `fix:`, `refactor:`)
- No socket.io, no Redux, no react-query — keep deps minimal

## Environment Variables

- `CF_ACCESS_TEAM_DOMAIN` — e.g. `myteam.cloudflareaccess.com`
- `CF_ACCESS_AUD` — Application Audience tag from CF dashboard
- `CF_ACCESS_ALLOWED_EMAILS` — comma-separated email allowlist

Without these, auth is bypassed (dev mode).

## Testing

```bash
cd backend && uv run pytest
```
