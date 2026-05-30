"""Persist bootstrap-acquired credentials in the worker volume.

After the user supplies an agent API key (or completes a device code
flow) through the hub's bootstrap UI, the worker stashes the result
here so subsequent container restarts don't have to re-prompt. The
agent SDK then rotates from this file as it normally would (Claude
SDK refreshes its OAuth tokens in place, for example).

Path: $CLAUDE_WEB_DB_PATH dir + "/credentials/<agent_type>.json".
Defaults to ~/.claude-web/credentials/<agent_type>.json inside the
container. File perms 0600.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


def _store_dir() -> Path:
    """Return the directory where credentials live for this worker."""
    db_path = os.environ.get("CLAUDE_WEB_DB_PATH")
    if db_path:
        # CLAUDE_WEB_DB_PATH is the SQLite file path; we co-locate the
        # credentials dir next to it so a single mounted volume keeps
        # both.
        base = Path(db_path).parent
    else:
        base = Path.home() / ".claude-web"
    out = base / "credentials"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _path(agent_type: str) -> Path:
    safe = "".join(c for c in agent_type if c.isalnum() or c in "-_")
    return _store_dir() / f"{safe}.json"


def save_credentials(
    agent_type: str, flow: str, data: dict[str, Any]
) -> None:
    """Persist a credentials blob to the worker volume.

    `data` shape is `flow`-dependent: for `api_key`, `{"api_key":
    "..."}`. For `device_code`, whatever the SDK gave us back
    (typically `{"access_token", "refresh_token", "expires_at"}`).
    """
    path = _path(agent_type)
    payload = {
        "agent_type": agent_type,
        "flow": flow,
        "data": data,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        # chmod not supported on every filesystem (mounted into a
        # container with permissions overridden); best-effort.
        pass
    tmp.replace(path)
    logger.info(
        "credentials_stored", agent_type=agent_type, flow=flow, path=str(path)
    )


def load_credentials(agent_type: str) -> dict[str, Any] | None:
    """Return the persisted credentials blob, or None if absent.

    Returned dict has the same shape as the `payload` argument to
    save_credentials (with the `flow`, `data`, `stored_at` keys).
    """
    path = _path(agent_type)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("credentials_load_failed", path=str(path), error=str(e))
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def clear_credentials(agent_type: str) -> bool:
    """Remove persisted credentials. Returns True if a file was removed."""
    path = _path(agent_type)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("credentials_clear_failed", path=str(path), error=str(e))
        return False
    logger.info("credentials_cleared", agent_type=agent_type)
    return True
