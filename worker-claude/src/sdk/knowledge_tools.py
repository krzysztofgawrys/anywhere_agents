"""Claude-side wiring of the shared knowledge module as in-process MCP.

Exposes `worker_shared.knowledge.service` as an SDK MCP server so the
agent can search and curate this project's knowledge base, plus the
automatic system-prompt directive that tells it to do so. The RAG logic
itself is SDK-agnostic and lives in `worker_shared/knowledge/`.

Tool names are kept short (server "kb", tools "search"/"save"/...) so the
fully-qualified `mcp__kb__search` form stays readable in the mobile UI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from worker_shared.db import db
from worker_shared.knowledge import service

logger = structlog.get_logger()

SERVER_NAME = "kb"

_TOOL_NAMES = ("search", "save", "list", "update", "delete")

# Automatic instruction appended to the Claude Code system prompt. Kept
# short so it does not bloat context on every turn.
KNOWLEDGE_DIRECTIVE = """
## Project knowledge base

This project has a persistent, agent-maintained knowledge base, available via the `kb` MCP \
tools (`search`, `save`, `list`, `update`, `delete`). Treat it as your long-term memory for \
THIS project.

- BEFORE answering questions about this project (architecture, decisions, conventions, \
where things live, known gotchas), call `search` to retrieve relevant notes instead of \
guessing or re-deriving from scratch.
- When you learn or produce something durably useful for FUTURE sessions - an architecture \
decision, a non-obvious gotcha, a stable API/contract, a hard-won how-to - call `save`. \
Prefer SEVERAL small, single-topic notes over one long entry: one focused fact/decision/ \
how-to per note, each just a few short paragraphs. Give each a clear title and 1-3 short \
comma-separated `tags` (e.g. 'build', 'gotcha', 'hardware', 'api').
- Be selective. Do NOT save transient, trivial, or easily-recomputed information (one-off \
command output, current file contents, ephemeral debugging state). Quality over quantity.
- Avoid duplicates: use `list` to see what exists, and `update` / `delete` to revise or \
remove stale notes rather than piling on near-duplicates.
""".strip()

# Prompt for the background consolidation turn (Phase 1b). The worker
# sends this after an idle period if the session did meaningful work.
CONSOLIDATION_PROMPT = (
    "[automatic consolidation] Review what happened in this session. If anything durably "
    "useful for future sessions on this project emerged - an architecture decision, a "
    "non-obvious gotcha, a stable contract, a hard-won how-to - save it now via the `kb` "
    "`save` tool. Prefer several small, single-topic entries (1-3 tags each) over one long "
    "note; skip transient/trivial detail; update or dedupe rather than piling on. If "
    "nothing qualifies, reply with a single short line and do not save."
)

OnChange = Callable[[], Awaitable[None]]


def tool_allow_patterns() -> list[str]:
    """Both bare and `mcp__server__tool` forms, for `allowed_tools` pre-approval."""
    out: list[str] = []
    for name in _TOOL_NAMES:
        out.append(name)
        out.append(f"mcp__{SERVER_NAME}__{name}")
    return out


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def build_knowledge_server(
    project_id: int, *, on_change: OnChange | None = None
) -> McpSdkServerConfig:
    """Build an in-process MCP server bound to *project_id*.

    `on_change` (optional) is awaited after a successful save/update/delete
    so the worker can push a `knowledge_updated` event to the UI.
    """

    async def _notify() -> None:
        if on_change is not None:
            try:
                await on_change()
            except Exception as exc:  # never let UI push break a tool call
                logger.warning("knowledge_on_change_failed", error=str(exc))

    @tool(
        "search",
        "Search THIS PROJECT's knowledge base for relevant notes saved by previous "
        "sessions. Call this BEFORE answering questions about the project.",
        {"query": str},
    )
    async def search(args: dict[str, Any]) -> dict[str, Any]:
        hits = await service.search(db, project_id, str(args.get("query", "")))
        if not hits:
            return _text("No matching project knowledge found.")
        blocks = [
            f"[{h['doc_id']}] {h['title']} (score {h['score']}):\n{h['text']}"
            for h in hits
        ]
        return _text("\n\n".join(blocks))

    @tool(
        "save",
        "Save ONE small, single-topic note to THIS PROJECT's knowledge base for future "
        "sessions. Be selective and granular: prefer several short focused notes over "
        "one long one. Add 1-3 short comma-separated tags.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short, specific title."},
                "content": {
                    "type": "string",
                    "description": "Concise, self-contained note (a few short paragraphs).",
                },
                "tags": {
                    "type": "string",
                    "description": "1-3 short comma-separated tags, e.g. 'build, gotcha'.",
                },
            },
            "required": ["title", "content"],
        },
    )
    async def save(args: dict[str, Any]) -> dict[str, Any]:
        raw_tags = args.get("tags")
        tags = str(raw_tags).strip() if raw_tags else None
        res = await service.save(
            db,
            project_id,
            str(args.get("title", "")),
            str(args.get("content", "")),
            tags=tags or None,
        )
        if res.get("saved"):
            await _notify()
            return _text(f"Saved knowledge entry #{res['id']}: {res['title']}")
        return _text(f"Not saved ({res.get('reason', 'unknown')}).")

    @tool(
        "list",
        "List existing entries in THIS PROJECT's knowledge base (id, title, dates).",
        {},
    )
    async def list_entries(args: dict[str, Any]) -> dict[str, Any]:
        entries = await service.list_entries(db, project_id)
        if not entries:
            return _text("The project knowledge base is empty.")
        lines = [
            f"[{e['id']}] {e['title']}  tags: {e['tags'] or '-'}  (updated {e['updated_at']})"
            for e in entries
        ]
        return _text("\n".join(lines))

    @tool(
        "update",
        "Update an existing entry by id: set new content and/or tags. Omit content "
        "to just (re)tag an entry in place (cheap, no re-embed) - e.g. to add tags to "
        "older untagged notes.",
        {
            "type": "object",
            "properties": {
                "doc_id": {"type": "integer"},
                "content": {
                    "type": "string",
                    "description": "New full content (omit to keep existing).",
                },
                "tags": {
                    "type": "string",
                    "description": "1-3 comma-separated tags (omit to keep existing).",
                },
            },
            "required": ["doc_id"],
        },
    )
    async def update(args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        content = args.get("content")
        if isinstance(content, str) and content.strip():
            kwargs["content"] = content
        if "tags" in args:
            kwargs["tags"] = str(args["tags"]).strip() or None
        res = await service.update(db, project_id, int(args["doc_id"]), **kwargs)
        if res.get("updated"):
            await _notify()
            changed = ", ".join(res.get("changed", []))
            return _text(f"Updated knowledge entry #{res['id']} ({changed}).")
        return _text(f"Not updated ({res.get('reason', 'unknown')}).")

    @tool(
        "delete",
        "Delete a stale or wrong knowledge entry by id.",
        {"doc_id": int},
    )
    async def delete(args: dict[str, Any]) -> dict[str, Any]:
        res = await service.delete(db, project_id, int(args["doc_id"]))
        if res.get("deleted"):
            await _notify()
            return _text(f"Deleted knowledge entry #{res['id']}.")
        return _text(f"Nothing deleted for id {res.get('id')}.")

    return create_sdk_mcp_server(
        SERVER_NAME,
        tools=[search, save, list_entries, update, delete],
    )
