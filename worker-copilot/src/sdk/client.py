"""Singleton CopilotClient lifecycle.

One CopilotClient per worker-copilot process. Spawns the bundled Copilot CLI
subprocess on first start; shared across all CopilotSessions created in this
worker (each frontend WS that opens a new_session adds a session to this
client, not a fresh subprocess).

Auth: defaults to `use_logged_in_user=True`, so the SDK reads credentials
from $COPILOT_HOME (mounted from host's ~/.copilot in the container). Set
COPILOT_GITHUB_TOKEN env to override.
"""

from __future__ import annotations

import os

import structlog
from copilot import CopilotClient, SubprocessConfig

logger = structlog.get_logger()


_client: CopilotClient | None = None


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
