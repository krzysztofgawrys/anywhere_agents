# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# Shared package (path-resolved from worker-claude/pyproject.toml as "../shared").
# Copied to /shared so the relative path resolves from /app/pyproject.toml.
# Source must be present at lock time because uv reads shared/pyproject.toml,
# AND at runtime because the editable install points at /shared/worker_shared.
COPY shared/ /shared/
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

# Pre-create the claude-web state directory with the runtime UID/GID
# baked in. When a named volume is bound at this path on first `up`,
# Docker copies the directory's ownership + permissions from the image
# into the (otherwise root-owned) volume - so the non-root worker
# process can write without a separate init-container chown service.
# Override APP_UID/APP_GID via `--build-arg` (or compose build.args)
# if the deploy host's UID isn't 1000.
ARG APP_UID=1000
ARG APP_GID=1000
RUN mkdir -p /home/app/.claude-web \
    && chown ${APP_UID}:${APP_GID} /home/app/.claude-web \
    && chmod 700 /home/app/.claude-web

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
# Shared package source - editable install in .venv points at /shared/worker_shared
COPY shared/ /shared/
COPY worker-claude/src/ /app/src/
COPY docker/entrypoint.sh /entrypoint.sh

# `fetch`: browser-impersonating HTTP client (curl_cffi) for agents. Plain
# `curl` is fingerprinted as a bot (JA3/JA4 TLS + HTTP/2) and silently reset by
# Akamai/Cloudflare-fronted sites (e.g. analog.com, mouser.com) regardless of
# headers. `fetch` mimics a real browser's TLS fingerprint and gets through.
# Installed to /usr/local (matches fetch's shebang); the app venv is separate.
RUN pip install --no-cache-dir curl_cffi
COPY docker/fetch /usr/local/bin/fetch
RUN chmod +x /usr/local/bin/fetch

ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8002
ENTRYPOINT ["/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8002"]
