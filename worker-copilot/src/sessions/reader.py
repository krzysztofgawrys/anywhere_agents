"""Read session metadata + messages from ~/.copilot/session-state/<uuid>/.

Two entry points:

- list_sessions(cwd): enumerate every session-state/<id>/workspace.yaml that
  has matching cwd. Returns the same {id, title, preview, message_count,
  mtime} shape worker-claude's reader produces, so the frontend's sidebar
  reuses its existing rendering.

- get_session_messages(cwd, session_id, limit, before_uuid): parse the
  session's events.jsonl, collapse the event stream into the frontend's
  turn-based ChatMessage[] shape (id, role, blocks). Same blocks worker-claude
  produces: text / thinking / tool / tool_result attached back onto its tool.

events.jsonl is line-delimited JSON, raw camelCase (Copilot's on-disk format
differs from the Python SDK's snake_case classes). We only need a small
subset of event types here:

  session.start            -> ignore (cwd already known from workspace.yaml)
  session.model_change     -> ignore for history
  system.message           -> ignore (CLI system prompt)
  user.message             -> user ChatMessage with `text` block (or `image`
                              attachments - skipped for v1)
  assistant.turn_start/end -> turn markers (no-op for our rendering)
  assistant.message        -> assistant text block (we use the consolidated
                              `content` field, not the streaming deltas)
  assistant.reasoning      -> thinking block
  tool.execution_start     -> tool block ({name, input})
  tool.execution_complete  -> attached to the matching tool block as
                              {result, is_error}
  permission.*             -> ignore (auto-resolved in history view)

Pagination: caller can pass `before_uuid` (event id of the oldest message
already shown) + `limit` (default 30). We compute the full message list,
trim to messages strictly before `before_uuid`, then take the latest `limit`.
Same semantics as worker-claude's reader.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


COPILOT_HOME = Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot")))
SESSION_STATE_ROOT = COPILOT_HOME / "session-state"


def _parse_workspace_yaml(path: Path) -> dict[str, str]:
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
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _safe_read_lines(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("events_jsonl_read_error", path=str(path), error=str(e))
    return out


def _count_user_messages(events_path: Path) -> int:
    """Approximate message count for the session list preview."""
    n = 0
    try:
        with events_path.open(encoding="utf-8") as f:
            for line in f:
                if '"type":"user.message"' in line:
                    n += 1
    except OSError:
        return 0
    return n


def _first_user_text(events_path: Path) -> str | None:
    """First user.message body, untransformed - used as preview text."""
    try:
        with events_path.open(encoding="utf-8") as f:
            for line in f:
                if '"type":"user.message"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("data", {}).get("content")
                if isinstance(content, str) and content:
                    return content
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text")
                            if isinstance(t, str) and t:
                                return t
    except OSError:
        return None
    return None


def list_sessions(
    cwd: str, session_state_root: Path = SESSION_STATE_ROOT
) -> list[dict[str, Any]]:
    """List Copilot sessions whose workspace.yaml cwd matches `cwd`."""
    if not session_state_root.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for entry in session_state_root.iterdir():
        if not entry.is_dir():
            continue
        ws = entry / "workspace.yaml"
        if not ws.is_file():
            continue
        meta = _parse_workspace_yaml(ws)
        if meta.get("cwd") != cwd:
            continue
        sid = meta.get("id") or entry.name
        events_path = entry / "events.jsonl"
        mtime = _iso_to_epoch(meta.get("updated_at", "")) or _iso_to_epoch(
            meta.get("created_at", "")
        )
        if mtime is None:
            try:
                mtime = events_path.stat().st_mtime if events_path.exists() else ws.stat().st_mtime
            except OSError:
                continue
        title = meta.get("summary") or None
        preview = _first_user_text(events_path) if events_path.exists() else None
        msg_count = _count_user_messages(events_path) if events_path.exists() else 0
        sessions.append({
            "id": sid,
            "title": title if title else None,
            "preview": (preview or "")[:160] if preview else None,
            "message_count": msg_count,
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def _convert_user_message(entry: dict[str, Any]) -> list[dict[str, Any]] | None:
    """user.message event -> list of frontend ChatBlocks."""
    data = entry.get("data", {})
    content = data.get("content")
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        return [{"kind": "text", "text": text}]
    blocks: list[dict[str, Any]] = []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "text":
                t = c.get("text")
                if isinstance(t, str) and t.strip():
                    blocks.append({"kind": "text", "text": t.strip()})
    return blocks or None


def get_session_messages(
    cwd: str,
    session_id: str,
    *,
    limit: int = 30,
    before_uuid: str | None = None,
    session_state_root: Path = SESSION_STATE_ROOT,
) -> dict[str, Any]:
    """Return the session's ChatMessage[] with the same shape worker-claude emits."""
    session_dir = session_state_root / session_id
    events_path = session_dir / "events.jsonl"
    if not events_path.is_file():
        return {"messages": [], "has_more": False, "oldest_uuid": None}

    raw = _safe_read_lines(events_path)

    messages: list[dict[str, Any]] = []
    # Track in-flight tool blocks so tool.execution_complete can attach result
    # onto the matching tool block in a prior assistant message.
    open_tools: dict[str, dict[str, Any]] = {}

    def _ensure_assistant_carrier(event_id: str) -> dict[str, Any]:
        """Get the trailing assistant message or create a fresh one."""
        if messages and messages[-1]["role"] == "assistant":
            return messages[-1]
        msg = {"id": event_id, "role": "assistant", "blocks": [], "finished": True}
        messages.append(msg)
        return msg

    for entry in raw:
        etype = entry.get("type")
        eid = entry.get("id") or ""
        data = entry.get("data", {})

        if etype == "user.message":
            blocks = _convert_user_message(entry)
            if not blocks:
                continue
            messages.append({
                "id": eid,
                "role": "user",
                "blocks": blocks,
                "finished": True,
            })
            continue

        if etype == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                carrier = _ensure_assistant_carrier(eid)
                carrier["blocks"].append({"kind": "text", "text": content.strip()})
            continue

        if etype == "assistant.reasoning":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                carrier = _ensure_assistant_carrier(eid)
                carrier["blocks"].append({"kind": "thinking", "text": content.strip()})
            continue

        if etype == "tool.execution_start":
            tool_use_id = data.get("toolCallId") or eid
            block = {
                "kind": "tool",
                "tool_use_id": tool_use_id,
                "name": data.get("toolName") or data.get("mcpToolName") or "tool",
                "input": data.get("arguments") or {},
            }
            open_tools[tool_use_id] = block
            carrier = _ensure_assistant_carrier(eid)
            carrier["blocks"].append(block)
            continue

        if etype == "tool.execution_complete":
            tool_use_id = data.get("toolCallId") or ""
            block = open_tools.pop(tool_use_id, None)
            if block is None:
                continue
            raw_result = data.get("result")
            raw_error = data.get("error")
            content = raw_result if raw_result is not None else raw_error
            if content is not None and not isinstance(content, (str, int, float, bool, list, dict)):
                content = str(content)
            block["result"] = content
            block["is_error"] = not bool(data.get("success", True))
            continue

        # Everything else (session.*, system.message, assistant.turn_*,
        # permission.*, assistant.streaming_delta, assistant.message_delta,
        # tool.execution_progress, etc.) is metadata or already covered by
        # the consolidated events above. Skip.

    # Pagination - mirror worker-claude/reader
    if before_uuid:
        cutoff = None
        for i, m in enumerate(messages):
            if m["id"] == before_uuid:
                cutoff = i
                break
        if cutoff is not None:
            messages = messages[:cutoff]
    has_more = False
    if limit > 0 and len(messages) > limit:
        has_more = True
        messages = messages[-limit:]
    oldest_uuid = messages[0]["id"] if messages else None
    return {"messages": messages, "has_more": has_more, "oldest_uuid": oldest_uuid}
