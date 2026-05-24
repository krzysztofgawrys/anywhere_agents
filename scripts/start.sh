#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# start.sh - one command to run Claude Web (production-like, no hot-reload).
#
# What it does:
#   1. Loads .env from project root (if present)
#   2. Installs/syncs backend deps (uv sync)
#   3. Installs frontend deps (npm ci / npm install)
#   4. Builds frontend → backend/src/static/
#   5. Runs pytest (optional, --skip-tests to skip)
#   6. Starts uvicorn on 127.0.0.1:8001
#
# Usage:
#   ./scripts/start.sh              # full flow
#   ./scripts/start.sh --skip-tests # skip pytest
#   ./scripts/start.sh --no-build   # skip frontend build + tests (fastest restart)
#
# For development with hot-reload, use ./scripts/dev.sh instead.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

SKIP_TESTS=false
NO_BUILD=false

for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=true ;;
        --no-build)   NO_BUILD=true; SKIP_TESTS=true ;;
        -h|--help)
            sed -n '2,/^set /{ /^#/s/^# \?//p }' "$0"
            exit 0 ;;
        *) echo "Unknown flag: $arg (use --help)"; exit 1 ;;
    esac
done

# ── Load .env ─────────────────────────────────────────────────────────────────
ENV_FILE="$ROOT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    echo "Loading $ENV_FILE"
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

# ── Prerequisite checks ──────────────────────────────────────────────────────
command -v uv  >/dev/null 2>&1 || { echo "ERROR: uv not found. Install: https://docs.astral.sh/uv/"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "ERROR: node not found."; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "ERROR: npm not found."; exit 1; }

# ── Backend deps ──────────────────────────────────────────────────────────────
echo "Syncing backend dependencies..."
cd "$BACKEND_DIR"
uv sync --quiet

if [[ "$NO_BUILD" == false ]]; then
    # ── Frontend deps + build ─────────────────────────────────────────────────
    echo "Installing frontend dependencies..."
    cd "$FRONTEND_DIR"
    if [[ -f package-lock.json ]]; then
        npm ci --silent 2>/dev/null || npm install --silent
    else
        npm install --silent
    fi

    echo "Building frontend → backend/src/static/ ..."
    npm run build --silent

    # ── Tests ─────────────────────────────────────────────────────────────────
    if [[ "$SKIP_TESTS" == false ]]; then
        echo "Running tests..."
        cd "$BACKEND_DIR"
        uv run pytest -q || { echo "Tests failed - fix before deploying."; exit 1; }
    fi
fi

# ── Start backend ─────────────────────────────────────────────────────────────
cd "$BACKEND_DIR"

echo ""
echo "┌─────────────────────────────────────────────┐"
echo "│  Claude Web running on http://127.0.0.1:8001 │"
if [[ -n "${CF_ACCESS_TEAM_DOMAIN:-}" ]]; then
    echo "│  CF Access:  $CF_ACCESS_TEAM_DOMAIN"
else
    echo "│  CF Access:  disabled (dev mode)"
fi
if [[ "${CLAUDE_WEB_ENV:-dev}" == "prod" || "${CLAUDE_WEB_ENV:-dev}" == "production" ]]; then
    echo "│  Environment: PRODUCTION (auth required)"
else
    echo "│  Environment: dev (auth bypassed)"
fi
echo "│  Press Ctrl+C to stop."
echo "└─────────────────────────────────────────────┘"
echo ""

exec uv run uvicorn src.main:app --host 127.0.0.1 --port 8001
