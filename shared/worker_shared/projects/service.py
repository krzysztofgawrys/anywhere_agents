"""Project service: query projects from DB."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from worker_shared.db import Database


async def list_projects(database: Database) -> list[dict[str, Any]]:
    """Return all registered projects, newest activity first.

    Sort order: most-recent session mtime DESC, NULLS LAST (projects with no
    sessions or unknown mtime fall to the bottom). Tie-break by name.
    """
    await database.init()
    async with database.connect() as conn:
        async with conn.execute(
            "SELECT id, path, name, auto_approve, created_at, last_seen_at, "
            "       last_session_mtime "
            "FROM projects "
            "ORDER BY last_session_mtime IS NULL, last_session_mtime DESC, name ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            projects = [
                {
                    "id": r["id"],
                    "path": r["path"],
                    "name": r["name"],
                    "auto_approve": bool(r["auto_approve"]),
                    "created_at": r["created_at"],
                    "last_seen_at": r["last_seen_at"],
                    "last_session_mtime": r["last_session_mtime"],
                    "available": Path(r["path"]).is_dir(),
                }
                for r in rows
            ]
            # Available projects first (sorted by activity), unavailable last
            projects.sort(key=lambda p: (not p["available"], 0))
            return projects


async def get_project(database: Database, project_id: int) -> dict[str, Any] | None:
    """Get a project by ID."""
    await database.init()
    async with database.connect() as conn:
        async with conn.execute(
            "SELECT id, path, name, auto_approve FROM projects WHERE id = ?",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "path": row["path"],
                "name": row["name"],
                "auto_approve": bool(row["auto_approve"]),
            }


async def create_project(database: Database, path: str) -> dict[str, Any]:
    """Register a directory as a project, returning the record (existing or new)."""
    from pathlib import Path

    resolved = str(Path(path).resolve())
    name = Path(resolved).name or resolved

    await database.init()
    async with database.connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO projects (path, name) VALUES (?, ?)",
            (resolved, name),
        )
        await conn.commit()
        async with conn.execute(
            "SELECT id, path, name, auto_approve, created_at, last_seen_at, "
            "       last_session_mtime FROM projects WHERE path = ?",
            (resolved,),
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "id": row["id"],
                "path": row["path"],
                "name": row["name"],
                "auto_approve": bool(row["auto_approve"]),
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "last_session_mtime": row["last_session_mtime"],
            }


async def set_auto_approve(
    database: Database, project_id: int, auto_approve: bool
) -> None:
    """Toggle per-project auto-approve setting."""
    await database.init()
    async with database.connect() as conn:
        await conn.execute(
            "UPDATE projects SET auto_approve = ? WHERE id = ?",
            (1 if auto_approve else 0, project_id),
        )
        await conn.commit()
