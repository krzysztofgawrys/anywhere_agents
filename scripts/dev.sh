#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cleanup() {
    echo "Stopping services..."
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
    wait $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting backend (FastAPI)..."
cd "$ROOT_DIR/backend"
uv run uvicorn src.main:app --reload --host 127.0.0.1 --port 8001 &
BACKEND_PID=$!

echo "Starting frontend (Vite)..."
cd "$ROOT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "Backend:  http://127.0.0.1:8001"
echo "Frontend: http://127.0.0.1:5173"
echo ""
echo "Press Ctrl+C to stop."

wait
