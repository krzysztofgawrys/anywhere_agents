"""Tests for project scanner and project service — Phase 3."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db import Database
from src.projects.scanner import project_dir_for_cwd, scan_and_register
from src.projects.service import (
    get_project,
    list_projects,
    set_auto_approve,
)


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test.sqlite")
    await db.init()
    return db


@pytest.fixture
def fake_projects_root(tmp_path: Path) -> Path:
    """Build a fake ~/.claude/projects/ tree."""
    root = tmp_path / "projects"
    root.mkdir()

    # Project A — has session with cwd inside
    project_a = root / "-home-alice-code-foo"
    project_a.mkdir()
    (project_a / "session1.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/home/alice/code/foo", "message": {"content": "hi"}})
        + "\n"
    )

    # Project B — empty session file (no cwd) — should be skipped or fall back
    project_b = root / "-home-bob-code-bar"
    project_b.mkdir()
    (project_b / "session2.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/home/bob/code/bar", "message": {"content": "hello"}})
        + "\n"
    )

    # Not a directory — should be ignored
    (root / "file.txt").write_text("ignore me")

    return root


async def test_scan_and_register_inserts_projects(
    database: Database, fake_projects_root: Path
) -> None:
    count = await scan_and_register(database, projects_root=fake_projects_root)
    assert count == 2

    projects = await list_projects(database)
    paths = {p["path"] for p in projects}
    assert "/home/alice/code/foo" in paths
    assert "/home/bob/code/bar" in paths


async def test_scan_is_idempotent(
    database: Database, fake_projects_root: Path
) -> None:
    await scan_and_register(database, projects_root=fake_projects_root)
    await scan_and_register(database, projects_root=fake_projects_root)
    projects = await list_projects(database)
    assert len(projects) == 2


async def test_get_project_by_id(
    database: Database, fake_projects_root: Path
) -> None:
    await scan_and_register(database, projects_root=fake_projects_root)
    projects = await list_projects(database)
    project = await get_project(database, projects[0]["id"])
    assert project is not None
    assert project["path"] in {"/home/alice/code/foo", "/home/bob/code/bar"}


async def test_set_auto_approve_toggle(
    database: Database, fake_projects_root: Path
) -> None:
    await scan_and_register(database, projects_root=fake_projects_root)
    projects = await list_projects(database)
    project_id = projects[0]["id"]
    assert projects[0]["auto_approve"] is False

    await set_auto_approve(database, project_id, True)
    refreshed = await get_project(database, project_id)
    assert refreshed is not None
    assert refreshed["auto_approve"] is True


def test_project_dir_for_cwd_encoding() -> None:
    """Project dirs encode '/' as '-'."""
    p = project_dir_for_cwd("/home/alice/code/foo", projects_root=Path("/tmp/x"))
    assert p == Path("/tmp/x/-home-alice-code-foo")


async def test_scan_missing_root_is_noop(database: Database, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    count = await scan_and_register(database, projects_root=missing)
    assert count == 0
