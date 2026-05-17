"""Tests for sessions/reader.py — list and read .jsonl session files."""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.sessions.reader import get_session_messages, list_sessions


def _write_session(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_list_sessions_extracts_title_and_preview(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    cwd = "/home/me/code/proj"
    project_dir = projects_root / "-home-me-code-proj"

    _write_session(
        project_dir / "abc-123.jsonl",
        [
            {"type": "ai-title", "aiTitle": "First chat about X"},
            {"type": "user", "message": {"content": "tell me about X"}, "uuid": "u1"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "X is..."}]},
                "uuid": "a1",
            },
        ],
    )

    sessions = list_sessions(cwd, projects_root=projects_root)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["id"] == "abc-123"
    assert s["title"] == "First chat about X"
    assert s["preview"] == "tell me about X"
    assert s["message_count"] == 2


def test_list_sessions_sorted_by_mtime(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    cwd = "/home/me/code/proj"
    project_dir = projects_root / "-home-me-code-proj"

    file_old = project_dir / "old.jsonl"
    file_new = project_dir / "new.jsonl"

    _write_session(file_old, [{"type": "user", "message": {"content": "old"}}])
    time.sleep(0.05)
    _write_session(file_new, [{"type": "user", "message": {"content": "new"}}])

    sessions = list_sessions(cwd, projects_root=projects_root)
    assert [s["id"] for s in sessions] == ["new", "old"]


def test_get_session_messages_pairs_tool_result_with_tool_call(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    cwd = "/home/me/code/proj"
    project_dir = projects_root / "-home-me-code-proj"

    _write_session(
        project_dir / "sess.jsonl",
        [
            {"type": "user", "message": {"content": "list files"}, "uuid": "u1"},
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "content": [
                        {"type": "text", "text": "Sure, let me check."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "uuid": "u2",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "file1\nfile2",
                            "is_error": False,
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "message": {"content": [{"type": "text", "text": "Found 2 files."}]},
            },
        ],
    )

    result = get_session_messages(cwd, "sess", projects_root=projects_root)
    messages = result["messages"]
    assert result["has_more"] is False
    # We expect 3 messages: u1, a1 (with tool block paired), a2
    # The u2 contained only tool_result blocks which are merged into a1.
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[0]["blocks"][0]["text"] == "list files"

    assert messages[1]["role"] == "assistant"
    blocks = messages[1]["blocks"]
    assert blocks[0]["kind"] == "text"
    assert blocks[1]["kind"] == "tool"
    assert blocks[1]["name"] == "Bash"
    assert blocks[1]["result"] == "file1\nfile2"
    assert blocks[1]["is_error"] is False

    assert messages[2]["role"] == "assistant"
    assert messages[2]["blocks"][0]["text"] == "Found 2 files."


def test_get_session_messages_respects_limit(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    cwd = "/home/me/code/proj"
    project_dir = projects_root / "-home-me-code-proj"

    entries = []
    for i in range(10):
        entries.append({
            "type": "user",
            "uuid": f"u{i}",
            "message": {"content": f"msg {i}"},
        })

    _write_session(project_dir / "sess.jsonl", entries)
    result = get_session_messages(cwd, "sess", limit=3, projects_root=projects_root)
    messages = result["messages"]
    assert len(messages) == 3
    assert result["has_more"] is True
    # Should be the last 3
    assert messages[0]["blocks"][0]["text"] == "msg 7"
    assert messages[2]["blocks"][0]["text"] == "msg 9"
    assert result["oldest_uuid"] == "u7"


def test_get_session_messages_before_uuid_returns_older_page(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    cwd = "/home/me/code/proj"
    project_dir = projects_root / "-home-me-code-proj"

    entries = [
        {"type": "user", "uuid": f"u{i}", "message": {"content": f"msg {i}"}}
        for i in range(10)
    ]
    _write_session(project_dir / "sess.jsonl", entries)

    # Page 2: before u7, limit 3 → should give u4, u5, u6
    result = get_session_messages(
        cwd, "sess", limit=3, before_uuid="u7", projects_root=projects_root
    )
    assert [m["id"] for m in result["messages"]] == ["u4", "u5", "u6"]
    assert result["has_more"] is True
    assert result["oldest_uuid"] == "u4"


def test_list_sessions_missing_project_returns_empty(tmp_path: Path) -> None:
    sessions = list_sessions("/nonexistent", projects_root=tmp_path / "projects")
    assert sessions == []
