"""Persist bootstrap-acquired credentials in the worker volume.

After the user supplies an agent API key (or completes a device code
flow) through the hub's bootstrap UI, the worker stashes the result
here so subsequent container restarts don't have to re-prompt. The
agent SDK then rotates from this file as it normally would (Claude
SDK refreshes its OAuth tokens in place, for example).

Path: $CLAUDE_WEB_DB_PATH dir + "/credentials/<agent_type>.enc".
Defaults to ~/.claude-web/credentials/<agent_type>.enc inside the
container. File perms 0600. Data is encrypted at rest using Fernet
symmetric encryption with a machine-local key.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import structlog
from cryptography.fernet import Fernet, InvalidToken

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


def _key_path() -> Path:
    return _store_dir() / ".keyfile"


def _get_fernet() -> Fernet:
    """Return a Fernet instance keyed to a machine-local secret.

    On first call the key is generated and persisted to `.keyfile`
    (mode 0600) alongside the credential files. Subsequent calls
    re-read the same key. The key never leaves the worker volume.
    """
    kp = _key_path()
    if kp.is_file():
        key = kp.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        kp.write_bytes(key)
        try:
            kp.chmod(0o600)
        except OSError:
            pass
    return Fernet(key)


def _path(agent_type: str) -> Path:
    safe = "".join(c for c in agent_type if c.isalnum() or c in "-_")
    return _store_dir() / f"{safe}.enc"


def _legacy_path(agent_type: str) -> Path:
    """Path used by the pre-encryption store (.json plaintext)."""
    safe = "".join(c for c in agent_type if c.isalnum() or c in "-_")
    return _store_dir() / f"{safe}.json"


def save_credentials(
    agent_type: str, flow: str, data: dict[str, Any]
) -> None:
    """Persist a credentials blob to the worker volume (encrypted).

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
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = _get_fernet().encrypt(plaintext)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(ciphertext)
    try:
        tmp.chmod(0o600)
    except OSError:
        # chmod not supported on every filesystem (mounted into a
        # container with permissions overridden); best-effort.
        pass
    tmp.replace(path)
    # Clean up any leftover plaintext file from the old format.
    _legacy_path(agent_type).unlink(missing_ok=True)
    logger.info(
        "credentials_stored", agent_type=agent_type, flow=flow, path=str(path)
    )


def load_credentials(agent_type: str) -> dict[str, Any] | None:
    """Return the persisted credentials blob, or None if absent.

    Returned dict has the same shape as the `payload` argument to
    save_credentials (with the `flow`, `data`, `stored_at` keys).

    Transparently migrates legacy plaintext .json files to the
    encrypted .enc format on first read.
    """
    path = _path(agent_type)

    # Try the encrypted file first.
    if path.is_file():
        try:
            ciphertext = path.read_bytes()
            plaintext = _get_fernet().decrypt(ciphertext)
            payload = json.loads(plaintext)
            if isinstance(payload, dict):
                return payload
        except (OSError, InvalidToken, json.JSONDecodeError) as e:
            logger.warning("credentials_load_failed", path=str(path), error=str(e))
            return None

    # Fall back to legacy plaintext .json and migrate it.
    legacy = _legacy_path(agent_type)
    if legacy.is_file():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("credentials_load_failed", path=str(legacy), error=str(e))
            return None
        if not isinstance(payload, dict):
            return None
        # Re-save encrypted and remove the plaintext file.
        save_credentials(
            agent_type=payload.get("agent_type", agent_type),
            flow=payload.get("flow", "unknown"),
            data=payload.get("data", {}),
        )
        logger.info("credentials_migrated_to_encrypted", agent_type=agent_type)
        return payload

    return None


def clear_credentials(agent_type: str) -> bool:
    """Remove persisted credentials. Returns True if a file was removed."""
    removed = False
    for p in (_path(agent_type), _legacy_path(agent_type)):
        try:
            p.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("credentials_clear_failed", path=str(p), error=str(e))
    if removed:
        logger.info("credentials_cleared", agent_type=agent_type)
    return removed
