"""SQLite database: schema, init, connection helpers.

Phase 3 scope: projects table only. Locks, settings extensions land in later phases.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()


DEFAULT_DB_PATH = Path(
    os.getenv("CLAUDE_WEB_DB_PATH", str(Path.home() / ".claude-web" / "db.sqlite"))
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    auto_approve INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_session_mtime REAL
);

CREATE INDEX IF NOT EXISTS idx_projects_path ON projects(path);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    model TEXT,
    effort TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
"""

# Inline migrations for pre-existing databases.
MIGRATIONS = [
    # Add last_session_mtime if missing
    "ALTER TABLE projects ADD COLUMN last_session_mtime REAL",
    # Add effort column to sessions (may not exist on first run)
    "ALTER TABLE sessions ADD COLUMN effort TEXT",
]


class Database:
    """Thin async wrapper around aiosqlite."""

    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path
        self._initialized = False

    async def init(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            await conn.executescript(SCHEMA)
            # Apply additive migrations - ignore errors (column may already exist)
            for stmt in MIGRATIONS:
                try:
                    await conn.execute(stmt)
                except Exception:
                    pass
            await conn.commit()
        self._initialized = True
        logger.info("db_initialized", path=str(self.path))

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()


    # ── Session preference helpers ────────────────────────────────────

    async def upsert_session(
        self,
        session_id: str,
        project_id: int,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        """Create or update a session row with model and/or effort."""
        async with self.connect() as conn:
            await conn.execute(
                """INSERT INTO sessions (id, project_id, model, effort, updated_at)
                   VALUES (?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                       model = excluded.model,
                       effort = excluded.effort,
                       updated_at = excluded.updated_at""",
                (session_id, project_id, model, effort),
            )
            await conn.commit()

    async def update_session_field(
        self,
        session_id: str,
        field: str,
        value: str | None,
    ) -> None:
        """Update a single field on an existing session row."""
        if field not in ("model", "effort"):
            raise ValueError(f"Invalid session field: {field}")
        async with self.connect() as conn:
            await conn.execute(
                f"UPDATE sessions SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
                (value, session_id),
            )
            await conn.commit()

    async def get_session_prefs(
        self, session_id: str
    ) -> tuple[str | None, str | None]:
        """Return (model, effort) for *session_id*, or (None, None)."""
        async with self.connect() as conn:
            row = await conn.execute_fetchall(
                "SELECT model, effort FROM sessions WHERE id = ?",
                (session_id,),
            )
            if row:
                return row[0][0], row[0][1]
            return None, None


# Module-level singleton; tests can override via dependency injection
db = Database()
