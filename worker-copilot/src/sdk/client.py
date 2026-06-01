"""Singleton CopilotClient lifecycle.

One CopilotClient per worker-copilot process. Spawns the bundled Copilot
CLI subprocess on first start; shared across all CopilotSessions created
in this worker (each frontend WS that opens a new_session adds a session
to this client, not a fresh subprocess).

Auth: defaults to `use_logged_in_user=True`, so the SDK reads credentials
from $COPILOT_HOME (mounted from host's ~/.copilot in the container).
Set COPILOT_GITHUB_TOKEN env to override (PAT with copilot scope) -
this is the headless / CI / Vault path.

If neither is present at start time, the bootstrap flow takes over:
worker emits `auth_needed` over the WS, user pastes a GitHub PAT in the
hub's modal, we persist it under ~/.claude-web/credentials/copilot.json
and hydrate COPILOT_GITHUB_TOKEN so the SDK picks it up. See
docs/bootstrap-auth-protocol.md for the protocol shape.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from copilot import CopilotClient, SubprocessConfig

from worker_shared.sdk.credentials_store import load_credentials

logger = structlog.get_logger()


_AGENT_TYPE = "copilot"
_BOOTSTRAP_INSTRUCTIONS = (
    "This worker has no Copilot credentials. Generate a GitHub "
    "fine-grained Personal Access Token at "
    "https://github.com/settings/personal-access-tokens/new with the "
    "`Copilot Requests` permission, paste it below (token starts with "
    "`github_pat_...`), and click Save. Classic tokens (`ghp_...`) are "
    "NOT accepted by the Copilot CLI. The token is sent through this "
    "hub to the worker once and persists in the worker's local volume."
)
# GitHub token prefixes (per docs/token-formats):
#   gho_         OAuth (Copilot CLI app, gh CLI)
#   ghu_         user-to-server OAuth
#   ghp_         classic PAT (still a real token even though `copilot`
#                rejects it - we'd rather honor a user's prior login
#                than re-prompt them only to fail at SDK start)
#   github_pat_  fine-grained PAT
# Any of these inside $COPILOT_HOME/config.json indicates a real prior
# login (vs. a fresh `{firstLaunchAt: ...}` config the SDK subprocess
# auto-creates on first start, which would otherwise trip the
# "directory has any file" naive heuristic into a false positive).
_TOKEN_MARKERS = ("gho_", "ghu_", "ghp_", "github_pat_")


_client: CopilotClient | None = None


def copilot_has_credentials() -> bool:
    """True iff the Copilot SDK can authenticate now.

    Side effect when bootstrap-persisted creds are found: hydrates the
    COPILOT_GITHUB_TOKEN env var so the next get_client() picks it up.

    Order: env var -> persistent bootstrap blob -> $COPILOT_HOME/config.json
    contains a GitHub token marker. The third path used to be "directory
    exists with any non-empty file" but that's a false positive: the SDK
    subprocess writes a stub config.json on first start with no token,
    so by the time `new_session` was clicked the naive check claimed
    "creds present" and the session then crashed mid-request with
    "Not authenticated". Now we look for the actual token substring.
    """
    if os.environ.get("COPILOT_GITHUB_TOKEN"):
        return True
    stored = load_credentials(_AGENT_TYPE)
    if stored is not None:
        data = stored.get("data") or {}
        token = data.get("github_token") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            os.environ["COPILOT_GITHUB_TOKEN"] = token
            return True
    # logged-in-user path: parse $COPILOT_HOME/config.json for a token
    # marker. The file is JSONC (has `//` comments) and the token is
    # nested under `copilotTokens.<host:user>` - a substring search is
    # both simpler and version-tolerant. SDK does real auth validation
    # at subprocess start anyway.
    copilot_home = Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot")))
    cfg = copilot_home / "config.json"
    if cfg.is_file():
        try:
            content = cfg.read_text(errors="ignore")
        except OSError:
            return False
        for marker in _TOKEN_MARKERS:
            if marker in content:
                return True
    return False


def copilot_bootstrap_instructions() -> str:
    """Free-text shown in the hub bootstrap modal above the input field."""
    return _BOOTSTRAP_INSTRUCTIONS


async def get_client() -> CopilotClient:
    """Return the process-wide CopilotClient, starting it lazily on first call."""
    global _client
    if _client is None:
        github_token = os.environ.get("COPILOT_GITHUB_TOKEN") or None
        config = SubprocessConfig(
            github_token=github_token,
        )
        client = CopilotClient(config=config, auto_start=False)
        await client.start()
        _client = client
        logger.info(
            "copilot_client_started",
            auth="env_token" if github_token else "logged_in_user",
        )
    return _client


async def reset_client() -> None:
    """Drop the cached client so the next get_client() reads fresh env.

    Called after bootstrap completes - the new COPILOT_GITHUB_TOKEN must
    take effect, but if get_client() already ran with the empty-token
    config it cached the wrong CopilotClient instance.
    """
    global _client
    if _client is None:
        return
    try:
        await _client.stop()
    except Exception as e:
        logger.warning("copilot_client_reset_stop_error", error=str(e))
    finally:
        _client = None
    logger.info("copilot_client_reset")


async def stop_client() -> None:
    """Stop the singleton CopilotClient. Called on worker shutdown."""
    global _client
    if _client is None:
        return
    try:
        await _client.stop()
    except Exception as e:
        logger.warning("copilot_client_stop_error", error=str(e))
    finally:
        _client = None
    logger.info("copilot_client_stopped")
