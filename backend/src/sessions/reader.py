"""Read session metadata + messages from ~/.claude/projects/<hash>/<session>.jsonl.

Phase 3 scope:
- list_sessions: enumerate .jsonl files in a project dir → list of {id, title,
  preview, message_count, mtime}
- get_session_messages: parse all user/assistant entries → uniform ChatBlock-like
  format for the frontend. (Pagination via `before`/`limit` is wired but for MVP
  we just return last N.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from src.projects.scanner import PROJECTS_ROOT, project_dir_for_cwd

logger = structlog.get_logger()


def _safe_read_lines(path: Path) -> list[dict[str, Any]]:
    """Read all JSONL lines, skipping malformed ones."""
    out: list[dict[str, Any]] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("session_read_error", path=str(path), error=str(e))
    return out


def _extract_title(entries: list[dict[str, Any]]) -> str | None:
    """Find the most recent ai-title entry."""
    for entry in reversed(entries):
        if entry.get("type") == "ai-title":
            title = entry.get("aiTitle")
            if isinstance(title, str) and title:
                return title
    return None


def _extract_first_user_text(entries: list[dict[str, Any]]) -> str | None:
    """Find the first user message text content."""
    for entry in entries:
        if entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text")
                    if isinstance(text, str):
                        return text
    return None


def list_sessions(cwd: str, projects_root: Path = PROJECTS_ROOT) -> list[dict[str, Any]]:
    """List sessions for a given project cwd.

    Returns list sorted by mtime desc (newest first).
    """
    project_dir = project_dir_for_cwd(cwd, projects_root)
    if not project_dir.is_dir():
        return []

    sessions = []
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            stat = jsonl.stat()
        except OSError:
            continue

        entries = _safe_read_lines(jsonl)
        title = _extract_title(entries)
        preview = _extract_first_user_text(entries)
        # Only count actual user/assistant message turns
        msg_count = sum(
            1 for e in entries if e.get("type") in ("user", "assistant")
        )

        sessions.append({
            "id": jsonl.stem,
            "title": title,
            "preview": (preview or "")[:160] if preview else None,
            "message_count": msg_count,
            "mtime": stat.st_mtime,
        })

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def _convert_block(block: Any) -> dict[str, Any] | None:
    """Convert a content block from .jsonl to ChatBlock-like dict."""
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype == "text":
        return {"kind": "text", "text": block.get("text", "")}
    if btype == "thinking":
        return {"kind": "thinking", "text": block.get("thinking", "")}
    if btype == "tool_use":
        return {
            "kind": "tool",
            "tool_use_id": block.get("id", ""),
            "name": block.get("name", ""),
            "input": block.get("input", {}),
        }
    if btype == "tool_result":
        # tool_result is a *user* message block referencing a prior tool_use_id
        return {
            "kind": "tool_result",
            "tool_use_id": block.get("tool_use_id", ""),
            "content": block.get("content"),
            "is_error": bool(block.get("is_error", False)),
        }
    return None


def get_session_messages(
    cwd: str,
    session_id: str,
    *,
    limit: int = 30,
    before_uuid: str | None = None,
    projects_root: Path = PROJECTS_ROOT,
) -> dict[str, Any]:
    """Return ChatMessage-like records for the frontend, with pagination.

    Returns: {"messages": [...], "has_more": bool, "oldest_uuid": str|None}
    - `before_uuid`: return only messages strictly *before* this uuid
    - `limit`: return at most this many messages (the LATEST `limit`)
    """
    project_dir = project_dir_for_cwd(cwd, projects_root)
    jsonl = project_dir / f"{session_id}.jsonl"
    if not jsonl.is_file():
        return {"messages": [], "has_more": False, "oldest_uuid": None}

    entries = _safe_read_lines(jsonl)

    # Build ChatMessage records — group consecutive blocks under one role
    messages: list[dict[str, Any]] = []
    for entry in entries:
        etype = entry.get("type")
        if etype not in ("user", "assistant"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue

        content = msg.get("content")
        blocks: list[dict[str, Any]] = []
        if isinstance(content, str):
            blocks.append({"kind": "text", "text": content})
        elif isinstance(content, list):
            for c in content:
                converted = _convert_block(c)
                if converted:
                    blocks.append(converted)

        if not blocks:
            continue

        # Handle tool_result blocks: attach to prior tool block instead of new message
        if etype == "user" and any(b["kind"] == "tool_result" for b in blocks):
            for b in blocks:
                if b["kind"] != "tool_result":
                    continue
                # Find matching tool block in prior messages
                for prev in reversed(messages):
                    matched = False
                    for pb in prev["blocks"]:
                        if pb["kind"] == "tool" and pb.get("tool_use_id") == b["tool_use_id"]:
                            pb["result"] = b.get("content")
                            pb["is_error"] = b.get("is_error", False)
                            matched = True
                            break
                    if matched:
                        break
            # Remove tool_result blocks from this user message
            blocks = [b for b in blocks if b["kind"] != "tool_result"]
            if not blocks:
                continue

        messages.append({
            "id": entry.get("uuid", ""),
            "role": etype,
            "blocks": blocks,
            "finished": True,
        })

    # Filter by before_uuid: only keep messages *before* that uuid
    if before_uuid:
        cutoff = None
        for i, m in enumerate(messages):
            if m["id"] == before_uuid:
                cutoff = i
                break
        if cutoff is not None:
            messages = messages[:cutoff]

    # Apply limit — return last N (chronologically the latest of the eligible set)
    has_more = False
    if limit > 0 and len(messages) > limit:
        has_more = True
        messages = messages[-limit:]

    oldest_uuid = messages[0]["id"] if messages else None
    return {"messages": messages, "has_more": has_more, "oldest_uuid": oldest_uuid}
