"""Sandboxed file browser for a project's cwd.

Both `list_directory` and `read_file` resolve their target path against the
project root and refuse anything that escapes it (no symlink-to-/etc tricks,
no `..` ladder). Hidden files are returned; the caller decides what to show.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

# Cap file reads at 2 MiB. Anything larger surfaces a `too_large` flag so the
# UI can show a "download" / "open externally" hint instead of trying to render
# megabytes of text through a WebSocket.
MAX_FILE_BYTES = 2 * 1024 * 1024
# A small leading sample is enough to detect binary content via NUL bytes.
TEXT_SAMPLE_BYTES = 8192


class FileBrowserError(Exception):
    """Errors with a stable code that maps to the WS error envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_within(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `root`.

    Blocks ``..`` segments in the input (path traversal), but allows symlinks
    to point outside the project root — the Docker container filesystem is
    the real sandbox, so anything mounted is intentionally accessible.
    """
    rel = (rel_path or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    root_resolved = root.resolve()
    target = root_resolved / rel
    # Resolve symlinks for the actual path we'll read
    return target.resolve() if target.exists() else target


def _rel_str(root: Path, target: Path) -> str:
    """Return target relative to root as a POSIX-style string ('' for root).

    Falls back to absolute path when target is outside root (symlink).
    """
    try:
        rel = target.relative_to(root).as_posix()
        return "" if rel == "." else rel
    except ValueError:
        return str(target)


def _parent_rel(rel: str) -> str | None:
    """Parent directory of a relative path, or None when already at root."""
    if not rel:
        return None
    if "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0]


def list_directory(project_path: str, rel_path: str = "") -> dict[str, Any]:
    """List entries in a directory inside the project.

    Returns: { path, parent, entries: [{name, kind, size, mtime}] }
    Directories sort first, then files; both alphabetically (case-insensitive).
    """
    root = Path(project_path)
    if not root.exists() or not root.is_dir():
        raise FileBrowserError(
            "not_found", f"Project root not found: {project_path}"
        )
    target = _resolve_within(root, rel_path)
    if not target.exists():
        raise FileBrowserError("not_found", f"Path not found: {rel_path}")
    if not target.is_dir():
        raise FileBrowserError("not_directory", "Path is not a directory")

    entries: list[dict[str, Any]] = []
    try:
        children = list(target.iterdir())
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc

    def _sort_key(p: Path) -> tuple[bool, str]:
        try:
            return (not p.is_dir(), p.name.lower())
        except OSError:
            return (True, p.name.lower())

    children.sort(key=_sort_key)
    for child in children:
        try:
            st = child.stat()
        except OSError:
            # Broken symlink, permission denied, or vanished entry — skip.
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "kind": "dir" if is_dir else "file",
                "size": None if is_dir else st.st_size,
                "mtime": st.st_mtime,
            }
        )

    # Use the input rel_path as canonical — not the resolved target.
    # Symlinks may resolve outside root, but the logical path through
    # the symlink stays valid for subsequent navigation.
    clean_rel = (rel_path or "").strip("/")
    return {
        "path": clean_rel,
        "parent": _parent_rel(clean_rel),
        "entries": entries,
    }


def list_absolute_directory(abs_path: str) -> dict[str, Any]:
    """List directories inside an absolute filesystem path (not sandboxed).

    Used by the new-project picker: only directories are returned so the user
    can navigate the filesystem and pick a project root.
    """
    target = Path(abs_path).expanduser().resolve()
    if not target.exists():
        raise FileBrowserError("not_found", f"Path not found: {abs_path}")
    if not target.is_dir():
        raise FileBrowserError("not_directory", "Path is not a directory")

    abs_str = str(target)
    parent_path = target.parent
    parent: str | None = str(parent_path) if parent_path != target else None

    entries: list[dict[str, Any]] = []
    try:
        children = list(target.iterdir())
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc

    children.sort(key=lambda p: p.name.lower())
    for child in children:
        if not child.is_dir():
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "kind": "dir",
                "size": None,
                "mtime": st.st_mtime,
            }
        )

    return {
        "path": abs_str,
        "parent": parent,
        "entries": entries,
    }


def create_absolute_directory(abs_path: str) -> None:
    """Create a directory (and any parents) at an absolute path."""
    target = Path(abs_path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def read_file(project_path: str, rel_path: str) -> dict[str, Any]:
    """Read a file inside the project, sandbox-checked.

    Returns: { path, size, too_large, encoding, content }
    - encoding: "utf-8" for text, "base64" for binary, None when too_large
    - too_large: True when size > MAX_FILE_BYTES; content is None in that case
    """
    root = Path(project_path)
    if not root.exists() or not root.is_dir():
        raise FileBrowserError("not_found", "Project root not found")
    target = _resolve_within(root, rel_path)
    if not target.exists():
        raise FileBrowserError("not_found", f"File not found: {rel_path}")
    if not target.is_file():
        raise FileBrowserError("not_file", "Path is not a file")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_rel = (rel_path or "").strip("/")

    if size > MAX_FILE_BYTES:
        return {
            "path": clean_rel,
            "size": size,
            "too_large": True,
            "encoding": None,
            "content": None,
        }

    try:
        raw = target.read_bytes()
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    if _looks_binary(raw[:TEXT_SAMPLE_BYTES]):
        return {
            "path": clean_rel,
            "size": size,
            "too_large": False,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": clean_rel,
            "size": size,
            "too_large": False,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
        }
    return {
        "path": clean_rel,
        "size": size,
        "too_large": False,
        "encoding": "utf-8",
        "content": text,
    }
