"""Scan ~/.copilot/session-state/ and register discovered projects in SQLite.

Skeleton phase: stub that initializes the DB but registers nothing. Phase 5
will iterate ~/.copilot/session-state/<session-id>/ subdirectories, read
each `workspace.yaml` (or equivalent metadata file) for the cwd, group by
cwd and upsert into the projects table.

Notes on what phase 5 needs to do:
- COPILOT_HOME env (default ~/.copilot) defines the root.
- Each session-state/<uuid>/ has `events.jsonl` and a workspace metadata file.
- workspace metadata likely contains `workspace_path` - the absolute cwd.
- Group sessions by workspace_path, upsert one project record per cwd,
  use `last_session_mtime` from the newest session's events.jsonl mtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from worker_shared.db import Database

logger = structlog.get_logger()


COPILOT_HOME = Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot")))
SESSION_STATE_ROOT = COPILOT_HOME / "session-state"


async def scan_and_register(database: Database, session_state_root: Path = SESSION_STATE_ROOT) -> int:
    """Skeleton: just init the DB. Phase 5 implements the real scan."""
    await database.init()
    if not session_state_root.is_dir():
        logger.warning("copilot_session_state_missing", path=str(session_state_root))
        return 0
    # Count sessions on disk just so we have a useful startup log line. We do
    # NOT register projects yet - that needs workspace metadata parsing which
    # lands in phase 5.
    session_count = sum(1 for entry in session_state_root.iterdir() if entry.is_dir())
    logger.info(
        "copilot_session_state_seen",
        path=str(session_state_root),
        session_dirs=session_count,
        registered_projects=0,
        note="phase 5 will parse workspace metadata and group by cwd",
    )
    return 0
