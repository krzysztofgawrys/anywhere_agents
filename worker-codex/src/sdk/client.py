"""Singleton Codex SDK client.

One Codex instance per worker-codex process. The SDK wraps the bundled
`codex` Rust CLI binary; threads are spawned per session and exchange
events over JSONL on stdin/stdout. Auth state is read from
~/.codex/auth.json (created by running `codex login` on the host;
mounted into the container).

If the SDK's installation step has not been run yet for the bundled
binary, get_client() raises a clear FileBrowserError-style message
instead of letting the underlying subprocess error bubble up.
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger()


# The openai-codex-sdk import is wrapped in a try so the worker still
# starts (and the /health endpoint stays green) even if the SDK isn't
# installed yet - new sessions will fail with a clear message rather
# than the whole worker container crashlooping.
try:
    from openai_codex_sdk import Codex  # type: ignore[import-not-found]

    _SDK_IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover - import guard
    Codex = None  # type: ignore[assignment,misc]
    _SDK_IMPORT_ERROR = str(_exc)


_client: object | None = None


def sdk_available() -> bool:
    """True iff openai-codex-sdk imported cleanly at startup."""
    return Codex is not None


def sdk_import_error() -> str | None:
    """The captured ImportError message, or None when the SDK is healthy."""
    return _SDK_IMPORT_ERROR


async def get_client() -> object:
    """Return the process-wide Codex client, starting it lazily on first call.

    Returns the raw openai_codex_sdk.Codex instance. Typed as `object`
    here to keep mypy --strict happy when the SDK is missing at import
    time; callers cast at the use site.
    """
    global _client
    if _client is None:
        if Codex is None:
            raise RuntimeError(
                f"openai-codex-sdk is not installed in this image: {_SDK_IMPORT_ERROR}"
            )
        env_overrides = {
            # Pass through any OPENAI_* env vars the user set in compose so
            # workspace-specific configuration (e.g. API base URL) reaches
            # the spawned CLI.
            k: v
            for k, v in os.environ.items()
            if k.startswith("OPENAI_")
        }
        config: dict[str, object] = {}
        if env_overrides:
            config["env"] = env_overrides
        _client = Codex(config) if config else Codex()
        logger.info(
            "codex_client_started",
            auth="logged_in_user",
            env_overrides=list(env_overrides.keys()),
        )
    return _client


async def stop_client() -> None:
    """No-op currently; openai-codex-sdk has no explicit shutdown hook.

    Threads are torn down individually by Session.stop(). Kept as a
    callable for symmetry with worker-copilot so the lifespan handler
    in main.py doesn't have to branch.
    """
    global _client
    _client = None
