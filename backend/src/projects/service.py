"""Project service: query projects from DB."""

from __future__ import annotations

from typing import Any

from src.db import Database


async def list_projects(database: Database) -> list[dict[str, Any]]:
    """Return all registered projects."""
    await database.init()
    async with database.connect() as conn:
        async with conn.execute(
            "SELECT id, path, name, auto_approve, created_at, last_seen_at FROM projects "
            "ORDER BY last_seen_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "path": r["path"],
                    "name": r["name"],
                    "auto_approve": bool(r["auto_approve"]),
                    "created_at": r["created_at"],
                    "last_seen_at": r["last_seen_at"],
                }
                for r in rows
            ]


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
