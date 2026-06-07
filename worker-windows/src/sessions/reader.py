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
import re
from pathlib import Path
from typing import Any

import structlog

from src.projects.scanner import PROJECTS_ROOT, project_dir_for_cwd
from src.sdk.session import parse_task_events, strip_task_events

# CLI-internal marker tags that the CLI silently injects into user-message
# text in .jsonl files. They are noise for the UI and should be removed.
_NOISE_TAGS = (
    "ide_opened_file",
    "ide_selection",
    "system-reminder",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "local-command-caveat",
    "local-command-stderr",
)

_NOISE_RE = re.compile(
    r"<(" + "|".join(re.escape(t) for t in _NOISE_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)


def _clean_user_text(text: str) -> str:
    """Strip CLI-injected marker blocks from a user message text body."""
    cleaned = _NOISE_RE.sub("", text)
    # Collapse runs of blank lines created by the removal.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

logger = structlog.get_logger()


def _safe_read_lines(path: Path) -> list[dict[str, Any]]:
    """Read all JSONL lines, skipping malformed ones."""
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
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
    """Find the first user message text content (post-cleaning of CLI markers)."""
    for entry in entries:
        if entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        raw: str | None = None
        if isinstance(content, str):
            raw = content
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text")
                    if isinstance(text, str):
                        raw = text
                        break
        if not raw:
            continue
        cleaned = _clean_user_text(raw)
        if cleaned:
            return cleaned
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


# Cap tool results at 20 KB in session_history responses. Large outputs
# (e.g. multi-hundred-KB git log, file contents) would balloon the WS payload
# to 10+ MB for a single page of 30 messages, triggering a websockets library
# drain assertion crash. The frontend already shows "output truncated" when
# results are huge - this just enforces it server-side so the WS survives.
_MAX_TOOL_RESULT_SIZE = 20_000  # characters


def _truncate_content(content: Any) -> Any:
    """Cap tool-result content so it doesn't blow up the WS frame."""
    if isinstance(content, str) and len(content) > _MAX_TOOL_RESULT_SIZE:
        return content[:_MAX_TOOL_RESULT_SIZE] + f"\n\n... (truncated, {len(content)} chars total)"
    if isinstance(content, list):
        total = sum(len(str(c)) for c in content)
        if total > _MAX_TOOL_RESULT_SIZE:
            # Flatten to a single truncated string for safety
            flat = "".join(
                c.get("text", str(c)) if isinstance(c, dict) else str(c)
                for c in content
            )
            return flat[:_MAX_TOOL_RESULT_SIZE] + f"\n\n... (truncated, {len(flat)} chars total)"
    return content


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
            "content": _truncate_content(block.get("content")),
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

    # Build ChatMessage records - group consecutive blocks under one role
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

        # Strip CLI marker tags from user text blocks; drop blocks left empty.
        # Also extract <task-notification> / <task-started> / <task-progress>
        # XML - those get attached to the preceding assistant message as `task`
        # blocks instead of leaking as raw blue user-bubble text.
        pending_task_blocks: list[dict[str, Any]] = []
        if etype == "user":
            cleaned_blocks: list[dict[str, Any]] = []
            for b in blocks:
                if b["kind"] == "text":
                    raw_text = b.get("text", "")
                    for ev in parse_task_events(raw_text):
                        pending_task_blocks.append({
                            "kind": "task",
                            "event_type": ev.get("event_type"),
                            "task_id": ev.get("task_id"),
                            "summary": ev.get("summary"),
                            "description": ev.get("description"),
                            "status": ev.get("status"),
                            "tool_use_id": ev.get("tool_use_id"),
                        })
                    cleaned = _clean_user_text(strip_task_events(raw_text))
                    if cleaned:
                        cleaned_blocks.append({"kind": "text", "text": cleaned})
                else:
                    cleaned_blocks.append(b)
            blocks = cleaned_blocks

        # Attach extracted task blocks to the preceding assistant message,
        # mirroring how tool_result is attached below.
        if pending_task_blocks:
            for prev in reversed(messages):
                if prev["role"] == "assistant":
                    prev["blocks"].extend(pending_task_blocks)
                    break
            else:
                # No preceding assistant - create a synthetic assistant carrier
                # so the events still appear in the timeline.
                messages.append({
                    "id": entry.get("uuid", "") + "-task",
                    "role": "assistant",
                    "blocks": pending_task_blocks,
                    "finished": True,
                })

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

        # Skip assistant messages with no visible content. The SDK can write
        # stub assistant entries to the JSONL (e.g. text="" placeholder while
        # streaming, or a partial message left behind when the worker was
        # killed mid-turn). Without this filter they render as empty grey
        # bubbles in the chat. A message is considered visible if it has at
        # least one tool/task/image block, or a text/thinking block with
        # non-whitespace content.
        if etype == "assistant":
            def _has_content(b: dict[str, Any]) -> bool:
                kind = b.get("kind")
                if kind in ("tool", "task", "image"):
                    return True
                if kind in ("text", "thinking"):
                    return bool((b.get("text") or "").strip())
                return False
            if not any(_has_content(b) for b in blocks):
                continue

        # Merge consecutive assistant entries into a single bubble. Live
        # streaming groups every tool_call / text / thinking event from the
        # same agent turn under one `currentAssistantId` until ResultMessage
        # arrives, so the chat shows one assistant bubble per turn. The SDK
        # writes each tool_use to its own JSONL entry though (with a
        # tool_result user entry in between, which the loop above strips),
        # so without this merge a turn with N tool calls splits into N+1
        # separate bubbles on history reload. Mirror the live grouping.
        if (
            etype == "assistant"
            and messages
            and messages[-1].get("role") == "assistant"
        ):
            messages[-1]["blocks"].extend(blocks)
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

    # Apply limit - return last N (chronologically the latest of the eligible set)
    has_more = False
    if limit > 0 and len(messages) > limit:
        has_more = True
        messages = messages[-limit:]

    oldest_uuid = messages[0]["id"] if messages else None
    return {"messages": messages, "has_more": has_more, "oldest_uuid": oldest_uuid}
