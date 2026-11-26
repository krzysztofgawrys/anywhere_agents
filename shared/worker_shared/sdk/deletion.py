"""Shared ``delete_session`` / ``delete_project`` WS command handlers.

The flow is the same for every worker type; only the on-disk transcript
layout differs, so each worker passes its own reader callables:

- ``list_sessions(project_path) -> list[dict]`` (entries carry ``id``)
- ``delete_session_files(project_path, session_id) -> None``
- ``delete_project_files(project_path) -> None``

Deleting a project only removes what the app stores about it (DB rows and
the agent's session transcripts). The project's working directory on disk is
never touched.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import structlog

from worker_shared.db import Database
from worker_shared.projects.service import delete_project, get_project, list_projects

logger = structlog.get_logger()

# Session ids are UUIDs (claude/copilot) or thread ids (codex). Anything with
# path separators or dots is rejected before it gets near the filesystem.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def is_safe_session_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.match(session_id))


async def _error(send: Any, code: str, message: str) -> None:
    await send({"type": "error", "payload": {"code": code, "message": message}})


async def apply_delete_session(
    payload: dict[str, Any],
    sessions: Any,
    send: Any,
    *,
    database: Database,
    delete_session_files: Callable[[str, str], None],
) -> None:
    """Stop a session, delete its transcript + prefs, emit ``session_deleted``."""
    project_id = payload.get("project_id")
    session_id = payload.get("session_id")
    if not isinstance(project_id, int) or not is_safe_session_id(session_id):
        await _error(send, "bad_request", "project_id and a valid session_id required")
        return
    project = await get_project(database, project_id)
    if not project:
        await _error(send, "not_found", f"project {project_id} not found")
        return
    if sessions.is_held_elsewhere(session_id):
        await _error(send, "session_in_use", "Session is open on another device - close it there first")
        return

    await sessions.discard_session(session_id)
    try:
        delete_session_files(project["path"], session_id)
    except OSError as e:
        logger.warning("session_delete_failed", session_id=session_id, error=str(e))
        await _error(send, "delete_failed", f"Could not delete session files: {e}")
        return
    await database.delete_session(session_id)
    logger.info("session_deleted", project_id=project_id, session_id=session_id)
    await send({
        "type": "session_deleted",
        "payload": {"project_id": project_id, "session_id": session_id},
    })


async def apply_delete_project(
    payload: dict[str, Any],
    sessions: Any,
    send: Any,
    *,
    database: Database,
    list_sessions: Callable[[str], list[dict[str, Any]]],
    delete_project_files: Callable[[str], None],
) -> None:
    """Stop the project's sessions, delete transcripts + DB rows, emit
    ``project_deleted`` followed by a fresh ``projects`` list."""
    project_id = payload.get("project_id")
    if not isinstance(project_id, int):
        await _error(send, "bad_request", "project_id required")
        return
    project = await get_project(database, project_id)
    if not project:
        await _error(send, "not_found", f"project {project_id} not found")
        return

    session_ids = {
        sid for it in list_sessions(project["path"])
        if is_safe_session_id(sid := it.get("id") or it.get("session_id"))
    }
    live = sessions.current
    if live is not None and sessions.current_project_id == project_id:
        session_ids.add(live.session_id)
    busy = [sid for sid in session_ids if sessions.is_held_elsewhere(sid)]
    if busy:
        await _error(
            send,
            "session_in_use",
            f"{len(busy)} session(s) of this project are open on another device - close them first",
        )
        return

    for sid in session_ids:
        await sessions.discard_session(sid)
    try:
        delete_project_files(project["path"])
    except OSError as e:
        logger.warning("project_delete_failed", project_id=project_id, error=str(e))
        await _error(send, "delete_failed", f"Could not delete session files: {e}")
        return
    await delete_project(database, project_id)
    logger.info("project_deleted", project_id=project_id, sessions=len(session_ids))
    await send({"type": "project_deleted", "payload": {"project_id": project_id}})
    await send({"type": "projects", "payload": {"projects": await list_projects(database)}})
