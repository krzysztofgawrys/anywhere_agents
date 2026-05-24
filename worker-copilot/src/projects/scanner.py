"""Scan ~/.copilot/session-state/ and register one project per cwd.

Each Copilot CLI session lives in $COPILOT_HOME/session-state/<uuid>/ and
ships with a tiny workspace.yaml that pins the session to a cwd:

    id: db702ce4-df8b-404a-8e1f-a143f1d1469c
    cwd: /home/kgawrys/code/Cop1
    summary_count: 0
    created_at: 2026-05-24T20:05:16.209Z
    updated_at: 2026-05-24T20:05:22.961Z
    summary: Wylistuj katalog

We walk every session-state subdir, parse workspace.yaml, group by cwd,
upsert one row in the `projects` table per cwd with the latest updated_at
across that cwd's sessions as `last_session_mtime`. Result: every directory
the user has ever used Copilot in shows up automatically in the sidebar,
the same way `~/.claude/projects/` powers worker-claude.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import structlog

from worker_shared.db import Database

logger = structlog.get_logger()


COPILOT_HOME = Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot")))
SESSION_STATE_ROOT = COPILOT_HOME / "session-state"


def _parse_workspace_yaml(path: Path) -> dict[str, str]:
    """Tiny key: value parser - workspace.yaml is flat and trusted."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    except OSError:
        return out
    return out


def _iso_to_epoch(iso: str) -> float | None:
    """Parse Copilot's ISO 8601 timestamps to a float epoch for SQLite."""
    if not iso:
        return None
    try:
        # Tolerate trailing Z; datetime.fromisoformat understands +00:00 only
        # on Python <3.11; ours is 3.13 but be safe.
        normalized = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


async def scan_and_register(
    database: Database, session_state_root: Path = SESSION_STATE_ROOT
) -> int:
    """Walk session-state/ and upsert one project per distinct cwd."""
    await database.init()
    if not session_state_root.is_dir():
        logger.warning("copilot_session_state_missing", path=str(session_state_root))
        return 0

    # cwd -> latest updated_at epoch across all of its sessions
    latest_mtime: dict[str, float] = {}
    seen_sessions = 0
    for entry in session_state_root.iterdir():
        if not entry.is_dir():
            continue
        ws = entry / "workspace.yaml"
        if not ws.is_file():
            continue
        meta = _parse_workspace_yaml(ws)
        cwd = meta.get("cwd")
        if not cwd:
            continue
        seen_sessions += 1
        mtime = _iso_to_epoch(meta.get("updated_at", "")) or _iso_to_epoch(
            meta.get("created_at", "")
        )
        if mtime is None:
            try:
                mtime = ws.stat().st_mtime
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
        "copilot_projects_scanned",
        registered=count,
        session_dirs_seen=seen_sessions,
        path=str(session_state_root),
    )
    return count
