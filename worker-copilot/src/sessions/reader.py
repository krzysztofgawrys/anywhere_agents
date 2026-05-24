"""Read session metadata + messages from ~/.copilot/session-state/<uuid>/events.jsonl.

Skeleton phase: stubs return empty results. Phase 5 implements the real
parsing of github-copilot-sdk's event format.

Phase 5 will need to:
- list_sessions(cwd): iterate session-state/, find sessions whose workspace
  metadata's workspace_path == cwd, return [{id, title, preview, message_count, mtime}].
- get_session_messages(cwd, session_id): parse session-state/<id>/events.jsonl,
  collapse the SessionEvent stream (assistant.message_delta, assistant.message,
  tool.execution_*, user.message, permission.*) into the UI's turn-based
  ChatMessage[] shape - the same one worker-claude's reader produces from
  Claude's .jsonl format.
"""

from __future__ import annotations

from typing import Any


def list_sessions(cwd: str) -> list[dict[str, Any]]:
    """Skeleton: no sessions until phase 5 parses workspace metadata."""
    return []


def get_session_messages(
    cwd: str,
    session_id: str,
    *,
    limit: int = 30,
    before_uuid: str | None = None,
) -> dict[str, Any]:
    """Skeleton: empty history until phase 5 parses events.jsonl."""
    return {"messages": [], "has_more": False, "oldest_uuid": None}
