"""Tests for session/project deletion: SessionManager.discard_session and the
shared delete_session / delete_project WS handlers."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from worker_shared.db import Database
from worker_shared.locks.manager import LockManager
from worker_shared.projects.service import create_project, get_project, list_projects
from worker_shared.sdk.deletion import (
    apply_delete_project,
    apply_delete_session,
    is_safe_session_id,
)

from tests.test_manager import _make_manager, _make_session


@pytest.fixture
async def database(tmp_path: Any) -> Database:
    d = Database(tmp_path / "test.sqlite")
    await d.init()
    return d


async def _noop(msg: Any) -> None:
    pass


def _codes(sent: list[dict]) -> list[str]:
    return [m["payload"]["code"] for m in sent if m["type"] == "error"]


# ── discard_session ─────────────────────────────────────────────────────


async def test_discard_active_session_stops_and_releases(tmp_path: Any) -> None:
    s = _make_session("a")
    mgr, _, lm, reg = _make_manager(sessions=[s])
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        await mgr.discard_session("a")
    assert mgr.current is None
    assert mgr.current_project_id is None
    s.stop.assert_awaited_once()
    assert lm.info("a") is None
    assert "a" not in reg


async def test_discard_parked_session_stops_it(tmp_path: Any) -> None:
    old, new = _make_session("old"), _make_session("new")
    mgr, _, _, reg = _make_manager(sessions=[old, new])
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path))
        await mgr.new_session(str(tmp_path))  # parks "old"
        assert "old" in reg
        await mgr.discard_session("old")
    assert "old" not in reg
    old.stop.assert_awaited_once()
    assert mgr.current is new
    new.stop.assert_not_awaited()


async def test_is_held_elsewhere() -> None:
    lm = LockManager()
    mgr, _, _, _ = _make_manager(lock_manager=lm)
    await lm.try_acquire(session_id="x", connection_id="other", device_label="phone", notify=_noop)
    await lm.try_acquire(session_id="y", connection_id="conn-1", device_label="test", notify=_noop)
    assert mgr.is_held_elsewhere("x")
    assert not mgr.is_held_elsewhere("y")
    assert not mgr.is_held_elsewhere("z")


def test_session_id_validation() -> None:
    assert is_safe_session_id("0b4c6f1e-2a3b-4c5d-8e9f-0123456789ab")
    for bad in ["", "../etc", "a/b", "a.jsonl", ".hidden", None, 5]:
        assert not is_safe_session_id(bad)


# ── apply_delete_session ────────────────────────────────────────────────


async def test_delete_session_removes_files_and_prefs(tmp_path: Any, database: Database) -> None:
    project = await create_project(database, str(tmp_path))
    await database.upsert_session("s1", project["id"], model="m")
    mgr, sent, _, reg = _make_manager()
    deleted: list[tuple[str, str]] = []
    with patch("worker_shared.sdk.manager.registry", reg):
        await apply_delete_session(
            {"project_id": project["id"], "session_id": "s1"}, mgr, mgr._send,
            database=database,
            delete_session_files=lambda path, sid: deleted.append((path, sid)),
        )
    assert deleted == [(project["path"], "s1")]
    assert await database.get_session_prefs("s1") == (None, None)
    assert sent[-1] == {
        "type": "session_deleted",
        "payload": {"project_id": project["id"], "session_id": "s1"},
    }


async def test_delete_session_refuses_when_open_elsewhere(tmp_path: Any, database: Database) -> None:
    project = await create_project(database, str(tmp_path))
    lm = LockManager()
    await lm.try_acquire(session_id="s1", connection_id="other", device_label="phone", notify=_noop)
    mgr, sent, _, _ = _make_manager(lock_manager=lm)
    deleted: list[Any] = []
    await apply_delete_session(
        {"project_id": project["id"], "session_id": "s1"}, mgr, mgr._send,
        database=database,
        delete_session_files=lambda *a: deleted.append(a),
    )
    assert deleted == []
    assert _codes(sent) == ["session_in_use"]


async def test_delete_session_rejects_path_traversal(tmp_path: Any, database: Database) -> None:
    project = await create_project(database, str(tmp_path))
    mgr, sent, _, _ = _make_manager()
    deleted: list[Any] = []
    await apply_delete_session(
        {"project_id": project["id"], "session_id": "../../x"}, mgr, mgr._send,
        database=database,
        delete_session_files=lambda *a: deleted.append(a),
    )
    assert deleted == []
    assert _codes(sent) == ["bad_request"]


# ── apply_delete_project ────────────────────────────────────────────────


async def test_delete_project_discards_sessions_and_rows(tmp_path: Any, database: Database) -> None:
    project = await create_project(database, str(tmp_path))
    await database.upsert_session("s1", project["id"])
    live = _make_session("live")
    mgr, sent, _, reg = _make_manager(sessions=[live])
    files_deleted: list[str] = []
    with patch("worker_shared.sdk.manager.registry", reg):
        await mgr.new_session(str(tmp_path), project_id=None)
        mgr._current_project_id = project["id"]  # avoid the db singleton in new_session
        await apply_delete_project(
            {"project_id": project["id"]}, mgr, mgr._send,
            database=database,
            list_sessions=lambda path: [{"id": "s1"}, {"id": "bad/../id"}],
            delete_project_files=files_deleted.append,
        )
    assert files_deleted == [project["path"]]
    live.stop.assert_awaited_once()
    assert mgr.current is None
    assert await get_project(database, project["id"]) is None
    assert await database.get_session_prefs("s1") == (None, None)
    assert [m["type"] for m in sent[-2:]] == ["project_deleted", "projects"]
    assert sent[-1]["payload"]["projects"] == await list_projects(database) == []
    # The project dir itself is never touched.
    assert tmp_path.is_dir()


async def test_delete_project_refuses_when_a_session_is_open_elsewhere(
    tmp_path: Any, database: Database
) -> None:
    project = await create_project(database, str(tmp_path))
    lm = LockManager()
    await lm.try_acquire(session_id="s2", connection_id="other", device_label="phone", notify=_noop)
    mgr, sent, _, _ = _make_manager(lock_manager=lm)
    files_deleted: list[str] = []
    await apply_delete_project(
        {"project_id": project["id"]}, mgr, mgr._send,
        database=database,
        list_sessions=lambda path: [{"id": "s1"}, {"id": "s2"}],
        delete_project_files=files_deleted.append,
    )
    assert files_deleted == []
    assert _codes(sent) == ["session_in_use"]
    assert await get_project(database, project["id"]) is not None
