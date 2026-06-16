# worker-codex Dockerfile - mirrors worker-copilot's structure.
#
# openai-codex-sdk wraps the `codex` Rust binary from OpenAI. The SDK
# can download and install that binary on demand via Codex.install(),
# but for reproducible images we install it ahead of time in the build
# step rather than at runtime. The install path matches the SDK's
# default (~/.codex/bin/codex inside the container).
#
# Exposes port 8004 (parallel to worker-claude:8002, worker-copilot:8003)
# so multiple workers can coexist on one host without port collisions.
# Reads source from worker-codex/, shares worker_shared/ via path dep.

# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# Shared package (path-resolved from worker-codex/pyproject.toml as "../shared").
COPY shared/ /shared/
COPY worker-codex/pyproject.toml ./
RUN mkdir -p src && touch src/__init__.py
RUN uv lock && uv sync --frozen --no-dev


# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim

# System deps: git, ssh, bash (terminal). curl is needed for the
# codex binary install fallback in the SDK if it ever has to
# re-download at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates git openssh-client bash \
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

RUN mkdir -p /home/app && chmod 777 /home/app \
    && chmod a+w /etc/passwd /etc/group

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY shared/ /shared/
COPY worker-codex/src/ /app/src/
COPY docker/entrypoint.sh /entrypoint.sh

# `fetch`: browser-impersonating HTTP client (curl_cffi) for agents. Plain
# `curl` is fingerprinted as a bot (JA3/JA4 TLS + HTTP/2) and silently reset by
# Akamai/Cloudflare-fronted sites regardless of headers. `fetch` mimics a real
# browser's TLS fingerprint and gets through. See docs/curl-akamai-bypass.md.
RUN pip install --no-cache-dir curl_cffi
COPY docker/fetch /usr/local/bin/fetch
RUN chmod +x /usr/local/bin/fetch

# Pre-fetch the codex CLI binary so cold starts inside the container
# don't have to. The SDK's Codex.install() is idempotent; if the
# upstream URL ever changes we get a clean build-time error here
# rather than a confusing first-prompt failure at runtime. Wrapped
# in a try so a transient network failure during image build doesn't
# wedge the whole image - the SDK can also fetch lazily at first use.
ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/home/app/.codex

RUN /app/.venv/bin/python -c "from openai_codex_sdk import Codex; Codex.install()" \
    || echo "codex install at build time failed; SDK will retry lazily on first session"

EXPOSE 8004
ENTRYPOINT ["/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8004/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8004"]
