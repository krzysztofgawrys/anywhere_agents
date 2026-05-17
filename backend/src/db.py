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
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_projects_path ON projects(path);
"""


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


# Module-level singleton; tests can override via dependency injection
db = Database()
