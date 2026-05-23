# ── Stage 1: Frontend build ──────────────────────────────────────────────────
FROM node:22-slim AS frontend

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npx vite build --outDir /build/static --emptyOutDir


# ── Stage 2: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY hub/pyproject.toml ./
# Minimal package source so uv sync can resolve the project entry
RUN mkdir -p src && touch src/__init__.py
RUN uv lock && uv sync --frozen --no-dev


# ── Stage 3: Runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim

# Hub is lightweight — no Node.js, no Claude CLI
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/app && chmod 777 /home/app \
    && chmod a+w /etc/passwd /etc/group

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY hub/src/ /app/src/
# Baked-in frontend build (fallback when no volume mount overrides it)
COPY --from=frontend /build/static /app/src/static/
COPY docker/entrypoint.sh /entrypoint.sh

ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8001
ENTRYPOINT ["/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/api/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]
