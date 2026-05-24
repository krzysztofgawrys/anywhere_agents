"""Scan ~/.claude/projects/ and register discovered projects in SQLite.

Layout: each subdirectory is one project. The directory name is the project's
absolute cwd with '/' → '-' (no escaping for '-' in original paths, so we can't
reverse it reliably). We treat the *most recent* user/assistant message in the
project as authoritative for the actual cwd.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from worker_shared.db import Database

logger = structlog.get_logger()


PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def _read_cwd_from_jsonl(jsonl_path: Path) -> str | None:
    """Read a session .jsonl and find the first record with a 'cwd' field."""
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _resolve_project_cwd(project_dir: Path) -> str | None:
    """Find the actual cwd for a project directory.

    Strategy: inspect the most recently modified .jsonl in the dir for a 'cwd'.
    Falls back to a naive decode of the directory name (replace '-' with '/').
    """
    jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for jsonl in jsonls:
        cwd = _read_cwd_from_jsonl(jsonl)
        if cwd:
            return cwd
    # Fallback: decode dir name
    name = project_dir.name
    if name.startswith("-"):
        return name.replace("-", "/", 1).replace("-", "/")
    return None


def _project_display_name(cwd: str | None, dir_name: str) -> str:
    """Human-friendly project name (basename of cwd, fallback to dir name)."""
    if cwd:
        return Path(cwd).name or cwd
    return dir_name


def _latest_session_mtime(project_dir: Path) -> float | None:
    """Return the mtime of the most recently modified .jsonl in this project."""
    latest: float | None = None
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            m = jsonl.stat().st_mtime
        except OSError:
            continue
        if latest is None or m > latest:
            latest = m
    return latest


async def scan_and_register(database: Database, projects_root: Path = PROJECTS_ROOT) -> int:
    """Scan ~/.claude/projects/ and upsert discovered projects.

    Returns the count of registered/updated projects.
    """
    await database.init()
    if not projects_root.is_dir():
        logger.warning("projects_root_missing", path=str(projects_root))
        return 0

    count = 0
    async with database.connect() as conn:
        for entry in sorted(projects_root.iterdir()):
            if not entry.is_dir():
                continue
            cwd = _resolve_project_cwd(entry)
            if not cwd:
                continue
            name = _project_display_name(cwd, entry.name)
            mtime = _latest_session_mtime(entry)
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

    logger.info("projects_scanned", count=count)
    return count


def project_dir_for_cwd(cwd: str, projects_root: Path = PROJECTS_ROOT) -> Path:
    """Return the ~/.claude/projects/ subdirectory for a given cwd.

    Claude CLI encodes cwd by replacing '/', '_' AND '.' all with '-'. This is
    a lossy hash - multiple cwds can collide - but the CLI accepts that because
    the real cwd is also stored inside every .jsonl entry.

    Example: `/home/me/code/claude_cloud` → `-home-me-code-claude-cloud`
             `/home/me/.claude-code-router` → `-home-me--claude-code-router`
    """
    encoded = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
    return projects_root / encoded
