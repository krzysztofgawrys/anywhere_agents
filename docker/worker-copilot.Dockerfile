# worker-copilot Dockerfile - mirrors worker-claude's structure but:
# - no Node.js, no @anthropic-ai/claude-code, no @github/copilot CLI install
#   (github-copilot-sdk wheel ships the Copilot CLI binary bundled, ~140MB)
# - exposes port 8003 (parallel to worker-claude on 8002)
# - reads its source from worker-copilot/, shares worker_shared/ via path dep

# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# Shared package (path-resolved from worker-copilot/pyproject.toml as "../shared").
# Same mechanism as worker-claude - source must be present at lock time AND
# at runtime because the editable install points at /shared/worker_shared.
COPY shared/ /shared/
COPY worker-copilot/pyproject.toml ./
RUN mkdir -p src && touch src/__init__.py
RUN uv lock && uv sync --frozen --no-dev


# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim

# System deps: git, ssh, bash (terminal). No Node here - github-copilot-sdk
# ships the Copilot CLI binary inside its wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates git openssh-client bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/*

RUN mkdir -p /home/app && chmod 777 /home/app \
    && chmod a+w /etc/passwd /etc/group

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
# Shared package source - editable install in .venv points at /shared/worker_shared
COPY shared/ /shared/
COPY worker-copilot/src/ /app/src/
COPY docker/entrypoint.sh /entrypoint.sh

ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    COPILOT_HOME=/home/app/.copilot

EXPOSE 8003
ENTRYPOINT ["/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8003/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8003"]
