"""Tests for hub.src.workers.registry - workers.json parsing."""

import json
import os
from pathlib import Path

import pytest

from src.workers.registry import WorkerInfo, load_workers


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write_config(path: Path, data: list | dict | str) -> str:
    f = path / "workers.json"
    if isinstance(data, str):
        f.write_text(data)
    else:
        f.write_text(json.dumps(data))
    return str(f)


# ── Happy path ─────────────────────────────────────────────────────────


def test_load_outbound_worker(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "w1", "type": "claude", "label": "Laptop", "url": "ws://localhost:8002/ws", "secret": "s"},
    ])
    workers = load_workers(p)
    assert len(workers) == 1
    w = workers[0]
    assert w.id == "w1"
    assert w.type == "claude"
    assert w.mode == "outbound"
    assert w.url == "ws://localhost:8002/ws"


def test_load_inbound_worker(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "remote", "type": "copilot", "label": "Remote", "mode": "inbound", "secret": "s"},
    ])
    workers = load_workers(p)
    assert len(workers) == 1
    w = workers[0]
    assert w.mode == "inbound"
    assert w.url == "inbound://remote"  # synthesized placeholder


def test_load_mixed(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "local", "url": "ws://w:8002/ws", "secret": "s"},
        {"id": "remote", "mode": "inbound", "secret": "s"},
    ])
    workers = load_workers(p)
    assert len(workers) == 2
    assert workers[0].mode == "outbound"
    assert workers[1].mode == "inbound"


# ── Defaults ───────────────────────────────────────────────────────────


def test_type_defaults_to_claude(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "w1", "url": "ws://x/ws", "secret": "s"},
    ])
    workers = load_workers(p)
    assert workers[0].type == "claude"


def test_label_defaults_to_id(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "my-worker", "url": "ws://x/ws", "secret": "s"},
    ])
    assert workers[0].label == "my-worker" if (workers := load_workers(p)) else False


# ── Validation / error handling ────────────────────────────────────────


def test_missing_file(config_dir: Path) -> None:
    workers = load_workers(str(config_dir / "nonexistent.json"))
    assert workers == []


def test_invalid_json(config_dir: Path) -> None:
    p = _write_config(config_dir, "not json {{{")
    workers = load_workers(p)
    assert workers == []


def test_not_array(config_dir: Path) -> None:
    p = _write_config(config_dir, {"id": "w1"})
    workers = load_workers(p)
    assert workers == []


def test_skip_entry_without_id(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"url": "ws://x/ws", "secret": "s"},
    ])
    workers = load_workers(p)
    assert workers == []


def test_skip_outbound_without_url(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "w1", "secret": "s"},  # outbound mode, no url
    ])
    workers = load_workers(p)
    assert workers == []


def test_skip_non_dict_entries(config_dir: Path) -> None:
    p = _write_config(config_dir, ["not a dict", 42, None])
    workers = load_workers(p)
    assert workers == []


def test_empty_type_falls_back(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "w1", "type": "", "url": "ws://x/ws", "secret": "s"},
    ])
    workers = load_workers(p)
    assert workers[0].type == "claude"


def test_unknown_mode_treated_as_outbound(config_dir: Path) -> None:
    p = _write_config(config_dir, [
        {"id": "w1", "mode": "banana", "url": "ws://x/ws", "secret": "s"},
    ])
    workers = load_workers(p)
    assert workers[0].mode == "outbound"
