"""Tests for the sandboxed project file browser."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from src.files.browser import (
    MAX_FILE_BYTES,
    FileBrowserError,
    list_directory,
    read_file,
)


def _make_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "src" / "lib").mkdir()
    (root / "src" / "lib" / "util.py").write_text("def x(): pass\n")
    (root / "README.md").write_text("# title\n")
    (root / "binary.bin").write_bytes(b"\x00\x01\x02\xff")


def test_list_root(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    result = list_directory(str(tmp_path))
    assert result["path"] == ""
    assert result["parent"] is None
    names = [e["name"] for e in result["entries"]]
    # dirs first, then files, both alphabetical
    # case-insensitive alpha: binary.bin sorts before README.md
    assert names == ["src", "binary.bin", "README.md"]
    src_entry = next(e for e in result["entries"] if e["name"] == "src")
    assert src_entry["kind"] == "dir"
    assert src_entry["size"] is None
    readme = next(e for e in result["entries"] if e["name"] == "README.md")
    assert readme["kind"] == "file"
    assert readme["size"] == len("# title\n")


def test_list_subdirectory(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    result = list_directory(str(tmp_path), "src")
    assert result["path"] == "src"
    assert result["parent"] == ""
    names = [e["name"] for e in result["entries"]]
    assert names == ["lib", "main.py"]


def test_list_rejects_escape(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    with pytest.raises(FileBrowserError) as exc:
        list_directory(str(tmp_path), "../outside")
    assert exc.value.code == "forbidden"


def test_list_missing_project(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileBrowserError) as exc:
        list_directory(str(missing))
    assert exc.value.code == "not_found"


def test_list_not_a_directory(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    with pytest.raises(FileBrowserError) as exc:
        list_directory(str(tmp_path), "README.md")
    assert exc.value.code == "not_directory"


def test_read_text_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    result = read_file(str(tmp_path), "README.md")
    assert result["encoding"] == "utf-8"
    assert result["too_large"] is False
    assert result["content"] == "# title\n"
    assert result["size"] == len("# title\n")


def test_read_binary_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    result = read_file(str(tmp_path), "binary.bin")
    assert result["encoding"] == "base64"
    assert result["too_large"] is False
    assert base64.b64decode(result["content"]) == b"\x00\x01\x02\xff"


def test_read_rejects_escape(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    with pytest.raises(FileBrowserError) as exc:
        read_file(str(tmp_path), "../etc/passwd")
    assert exc.value.code == "forbidden"


def test_read_missing_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    with pytest.raises(FileBrowserError) as exc:
        read_file(str(tmp_path), "nope.txt")
    assert exc.value.code == "not_found"


def test_read_not_a_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    with pytest.raises(FileBrowserError) as exc:
        read_file(str(tmp_path), "src")
    assert exc.value.code == "not_file"


def test_read_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.txt"
    # Write something slightly bigger than the limit. Cap small for the test.
    monkeypatch.setattr("src.files.browser.MAX_FILE_BYTES", 16)
    big.write_text("x" * 32)
    result = read_file(str(tmp_path), "big.txt")
    assert result["too_large"] is True
    assert result["content"] is None
    assert result["encoding"] is None
    assert result["size"] == 32
    # sanity: real constant is much larger than the patched one
    assert MAX_FILE_BYTES > 16
