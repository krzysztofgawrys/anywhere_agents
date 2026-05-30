"""Scan ~/.codex/sessions/ and register one project per cwd.

The openai-codex-sdk persists thread state under
$CODEX_HOME/sessions/<thread_id>/ (default $CODEX_HOME = ~/.codex).
Exact on-disk layout is an SDK internal that has not been frozen;
this scanner reads it defensively:

- Walk every subdirectory of sessions/.
- For each candidate session, look for a metadata file. Observed
  variants across SDK versions:
    metadata.json   { "working_directory": "/...", "updated_at": "...", ... }
    workspace.json  same shape as Copilot
    state.json      catch-all
- The first match that has a working_directory string is used.
- Sessions without a recoverable cwd are skipped (they still work
  for direct resume by id but don't get a sidebar entry).

This is intentionally permissive: when the Codex SDK eventually
ships a documented session-history format, swap the per-file
inspection for a single typed parse. Until then, missing fields
just mean a smaller projects list, never a worker crash.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from worker_shared.db import Database

logger = structlog.get_logger()


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
SESSIONS_ROOT = CODEX_HOME / "sessions"

# Candidate metadata file names, in priority order.
_META_FILENAMES: tuple[str, ...] = (
    "metadata.json",
    "workspace.json",
    "state.json",
    "session.json",
)

# Field names that may carry the project root path.
_CWD_FIELDS: tuple[str, ...] = (
    "working_directory",
    "cwd",
    "workspace_path",
    "workspace",
    "project_root",
)

# Field names that may carry the last-updated timestamp.
_MTIME_FIELDS: tuple[str, ...] = (
    "updated_at",
    "last_updated",
    "modified_at",
    "created_at",
)


def _read_metadata(session_dir: Path) -> dict[str, Any] | None:
    """Return the first parseable metadata blob from a session directory."""
    for name in _META_FILENAMES:
        candidate = session_dir / name
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _iso_to_epoch(iso: str) -> float | None:
    if not iso:
        return None
    try:
        normalized = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _extract_cwd(meta: dict[str, Any]) -> str | None:
    for field in _CWD_FIELDS:
        value = meta.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_mtime(meta: dict[str, Any]) -> float | None:
    for field in _MTIME_FIELDS:
        value = meta.get(field)
        if isinstance(value, str):
            epoch = _iso_to_epoch(value)
            if epoch is not None:
                return epoch
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return None


async def scan_and_register(
    database: Database, sessions_root: Path = SESSIONS_ROOT
) -> int:
    """Walk sessions/ and upsert one project per distinct cwd."""
    await database.init()
    if not sessions_root.is_dir():
        logger.info(
            "codex_sessions_missing",
            path=str(sessions_root),
            hint="Run `codex login` and start a thread to populate this directory.",
        )
        return 0

    latest_mtime: dict[str, float] = {}
    seen_sessions = 0
    for entry in sessions_root.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_metadata(entry)
        if meta is None:
            continue
        cwd = _extract_cwd(meta)
        if not cwd:
            continue
        seen_sessions += 1
        mtime = _extract_mtime(meta)
        if mtime is None:
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
        existing = latest_mtime.get(cwd)
        if existing is None or mtime > existing:
            latest_mtime[cwd] = mtime

    count = 0
    async with database.connect() as conn:
        for cwd, mtime in latest_mtime.items():
            name = Path(cwd).name or cwd
            await conn.execute(
                """
                INSERT INTO projects (path, name, last_seen_at, last_session_mtime)
                VALUES (?, ?, datetime('now'), ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    last_seen_at = datetime('now'),
                    last_session_mtime = excluded.last_session_mtime
                """,
                (cwd, name, mtime),
            )
            count += 1
        await conn.commit()

    logger.info(
        "codex_projects_scanned",
        registered=count,
        session_dirs_seen=seen_sessions,
        path=str(sessions_root),
    )
    return count
