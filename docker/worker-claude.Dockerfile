# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY worker-claude/pyproject.toml ./
RUN mkdir -p src && touch src/__init__.py
RUN uv lock && uv sync --frozen --no-dev


# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim

# System deps: git, ssh, bash (terminal), Node.js + Claude CLI
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git openssh-client bash \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /root/.npm

RUN mkdir -p /home/app && chmod 777 /home/app \
    && chmod a+w /etc/passwd /etc/group

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY worker-claude/src/ /app/src/
COPY docker/entrypoint.sh /entrypoint.sh

ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8002
ENTRYPOINT ["/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8002"]
