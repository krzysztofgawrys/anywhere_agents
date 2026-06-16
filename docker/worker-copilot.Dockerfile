# worker-copilot Dockerfile - mirrors worker-claude's structure.
# Installs Node.js + @github/copilot CLI (npm) for OAuth device-flow login
# that reliably writes tokens to config.json. The Python SDK
# (github-copilot-sdk) still provides the runtime; the npm CLI is used
# only for the `copilot login` credential bootstrap.
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

# System deps: git, ssh, bash (terminal), Node.js (for @github/copilot CLI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates git openssh-client bash \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# GitHub CLI (`gh`): used by agents to read Dependabot alerts, manage PRs/issues
# and call the GitHub API. Installed from the official release tarball so we
# avoid the apt-repo + gnupg keyring dance on slim. `dpkg --print-architecture`
# yields amd64/arm64, matching gh's release asset naming.
ARG GH_VERSION=2.94.0
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" -o /tmp/gh.tar.gz \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && mv "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${arch}" \
    && gh --version

# Install the standalone Copilot CLI (npm). The SDK-bundled binary
# (v1.0.36) does NOT write tokens to config.json after `copilot login`;
# the npm CLI (>=1.0.51) does. Used only for the OAuth bootstrap flow.
RUN npm install -g @github/copilot@latest \
    && npm cache clean --force

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

RUN mkdir -p /home/app/.copilot \
    && chown ${APP_UID}:${APP_GID} /home/app/.copilot \
    && chmod 700 /home/app/.copilot

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
# Shared package source - editable install in .venv points at /shared/worker_shared
COPY shared/ /shared/
COPY worker-copilot/src/ /app/src/
COPY docker/entrypoint.sh /entrypoint.sh

# `fetch`: browser-impersonating HTTP client (curl_cffi) for agents. Plain
# `curl` is fingerprinted as a bot (JA3/JA4 TLS + HTTP/2) and silently reset by
# Akamai/Cloudflare-fronted sites regardless of headers. `fetch` mimics a real
# browser's TLS fingerprint and gets through. See docs/curl-akamai-bypass.md.
RUN pip install --no-cache-dir curl_cffi
COPY docker/fetch /usr/local/bin/fetch
RUN chmod +x /usr/local/bin/fetch

ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    COPILOT_HOME=/home/app/.copilot

EXPOSE 8003
ENTRYPOINT ["/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8003/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8003"]
