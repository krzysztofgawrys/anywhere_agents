"""Read Codex session histories from ~/.codex/sessions/<thread_id>/.

openai-codex-sdk does not (yet) publish a stable on-disk transcript
format. This module returns empty placeholders so the WS protocol
(`list_sessions`, `session_history`) keeps working without crashing,
and surfaces a clear notice in the response so the frontend can show
"history not available" instead of an infinite spinner.

When the SDK eventually documents the layout (or we agree to parse
the current internal one), swap the implementations below to return
real session lists and message arrays. The function signatures match
worker-claude and worker-copilot so the WS handler can stay generic.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
SESSIONS_ROOT = CODEX_HOME / "sessions"


def list_sessions(project_path: str) -> list[dict[str, Any]]:
    """Return the list of past Codex threads rooted at `project_path`.

    Best-effort: walks sessions/, peeks at each metadata file, and
    returns those whose working_directory matches. Returns an empty
    list when sessions/ is missing.
    """
    if not SESSIONS_ROOT.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        meta_path = next(
            (
                entry / name
                for name in ("metadata.json", "workspace.json", "state.json", "session.json")
                if (entry / name).is_file()
            ),
            None,
        )
        if meta_path is None:
            continue
        try:
            import json as _json
            data = _json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, Exception):
            continue
        if not isinstance(data, dict):
            continue
        cwd = (
            data.get("working_directory")
            or data.get("cwd")
            or data.get("workspace_path")
        )
        if cwd != project_path:
            continue
        out.append({
            "session_id": entry.name,
            "summary": data.get("summary") or data.get("title") or "",
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at") or data.get("modified_at"),
        })

    # Newest first; missing timestamps sort to the back.
    out.sort(key=lambda s: (s.get("updated_at") or s.get("created_at") or ""), reverse=True)
    return out


def get_session_messages(
    project_path: str,
    session_id: str,
    *,
    limit: int = 30,
    before_uuid: str | None = None,
) -> dict[str, Any]:
    """Return paginated chat history for a Codex thread.

    Currently a stub: openai-codex-sdk does not expose a stable
    on-disk transcript format. Returns an empty messages list with
    `has_more=False` plus a one-line system notice in the messages
    array so the UI shows "history not available yet for Codex
    workers" instead of a blank chat with no explanation.
    """
    del project_path, session_id, limit, before_uuid
    logger.debug("codex_session_history_stub")
    return {
        "messages": [
            {
                "type": "system",
                "uuid": "codex-history-notice",
                "subtype": "info",
                "data": {
                    "text": (
                        "Session history is not yet available for "
                        "worker-codex. The thread will continue from "
                        "wherever Codex left it on disk once you send "
                        "a new prompt."
                    ),
                },
            }
        ],
        "has_more": False,
        "oldest_uuid": None,
    }
