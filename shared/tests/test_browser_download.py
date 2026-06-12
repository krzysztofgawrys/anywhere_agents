"""Tests for the download/zip helpers in worker_shared.files.browser.

Focus: the sandbox boundary (path traversal, escaping symlinks) and correct
archive-name flattening, since these feed browser-triggered HTTP downloads.
"""

import io
import os
import zipfile
from pathlib import Path

import pytest

from worker_shared.files import FileBrowserError, build_zip, resolve_download_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small project tree:  root/{a.txt, sub/b.txt, sub/c.txt}."""
    (tmp_path / "a.txt").write_text("alpha")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("bravo")
    (sub / "c.txt").write_text("charlie")
    return tmp_path


# ── resolve_download_path ──────────────────────────────────────────────


def test_resolve_returns_abs_path_and_size(project: Path) -> None:
    info = resolve_download_path(str(project), "a.txt")
    assert info["name"] == "a.txt"
    assert info["size"] == len("alpha")
    assert info["abs_path"] == os.path.realpath(str(project / "a.txt"))


def test_resolve_rejects_traversal(project: Path) -> None:
    with pytest.raises(FileBrowserError) as exc:
        resolve_download_path(str(project), "../secret")
    assert exc.value.code == "forbidden"


def test_resolve_rejects_directory(project: Path) -> None:
    with pytest.raises(FileBrowserError) as exc:
        resolve_download_path(str(project), "sub")
    assert exc.value.code == "not_file"


def test_resolve_rejects_escaping_symlink(project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(FileBrowserError) as exc:
        resolve_download_path(str(project), "link.txt")
    assert exc.value.code == "forbidden"


# ── build_zip ──────────────────────────────────────────────────────────


def _names(buf: io.BytesIO) -> list[str]:
    buf.seek(0)
    return sorted(zipfile.ZipFile(buf).namelist())


def test_zip_files_and_dir_recursive(project: Path) -> None:
    buf = io.BytesIO()
    n = build_zip(str(project), "", ["a.txt", "sub"], buf)
    assert n == 3
    assert _names(buf) == ["a.txt", "sub/b.txt", "sub/c.txt"]


def test_zip_flattens_relative_to_base(project: Path) -> None:
    # Selection made inside "sub" -> archive names are stripped of the base.
    buf = io.BytesIO()
    build_zip(str(project), "sub", ["sub/b.txt", "sub/c.txt"], buf)
    assert _names(buf) == ["b.txt", "c.txt"]


def test_zip_empty_selection_rejected(project: Path) -> None:
    with pytest.raises(FileBrowserError) as exc:
        build_zip(str(project), "", [], io.BytesIO())
    assert exc.value.code == "bad_request"


def test_zip_rejects_traversal(project: Path) -> None:
    with pytest.raises(FileBrowserError) as exc:
        build_zip(str(project), "", ["../etc"], io.BytesIO())
    assert exc.value.code == "forbidden"


def test_zip_skips_escaping_symlink_in_dir(project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    link = project / "sub" / "evil.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported on this platform")
    buf = io.BytesIO()
    build_zip(str(project), "", ["sub"], buf)
    names = _names(buf)
    # The real files are present; the escaping symlink is silently skipped.
    assert "sub/b.txt" in names
    assert "sub/c.txt" in names
    assert "sub/evil.txt" not in names
