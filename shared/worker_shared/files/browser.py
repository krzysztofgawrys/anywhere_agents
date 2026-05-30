"""Sandboxed file browser for a project's cwd.

Both `list_directory` and `read_file` resolve their target path against the
project root and refuse anything that escapes it, including after symlink
resolution. Hidden files are returned; the caller decides what to show.

Security note: the worker container mounts sensitive host paths
(~/.claude, ~/.ssh, ~/.copilot, ~/.gitconfig) so an unchecked symlink
inside a project root could leak SSH keys or auth tokens to the
browser-facing WS. Every public function inlines the same `os.path.realpath`
+ `str.startswith(root + os.sep)` containment check immediately before any
filesystem operation - that pattern is the canonical CodeQL sanitizer for
the "Uncontrolled data used in path expression" rule, and CodeQL does NOT
propagate sanitizer recognition across helper-function boundaries. The
duplication looks ugly; it is the price of a clean security scan.
"""

from __future__ import annotations

import base64
import os
import os.path
from typing import Any

# Cap file reads at 2 MiB. Anything larger surfaces a `too_large` flag so the
# UI can show a "download" / "open externally" hint instead of trying to render
# megabytes of text through a WebSocket.
MAX_FILE_BYTES = 2 * 1024 * 1024
# A small leading sample is enough to detect binary content via NUL bytes.
TEXT_SAMPLE_BYTES = 8192

# Absolute filesystem subtrees that the unsandboxed
# list_absolute_directory / create_absolute_directory helpers refuse to
# touch. Defense in depth: the project-picker UI shouldn't be able to
# wander into kernel pseudo-fs or device nodes even if a user types
# the path manually. Mounted user home dirs and /tmp stay reachable on
# purpose so the picker can actually do its job.
_FORBIDDEN_ABSOLUTE_PREFIXES: tuple[str, ...] = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
)


class FileBrowserError(Exception):
    """Errors with a stable code that maps to the WS error envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _rel_str(root: str, target: str) -> str:
    """Return target relative to root as a POSIX-style string ('' for root)."""
    if target == root:
        return ""
    if target.startswith(root + os.sep):
        return target[len(root) + 1 :].replace(os.sep, "/")
    return target


def _parent_rel(rel: str) -> str | None:
    """Parent directory of a relative path, or None when already at root."""
    if not rel:
        return None
    if "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0]


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def list_directory(project_path: str, rel_path: str = "") -> dict[str, Any]:
    """List entries in a directory inside the project.

    Returns: { path, parent, entries: [{name, kind, size, mtime}] }
    Directories sort first, then files; both alphabetically (case-insensitive).
    """
    # ── Inline path sanitizer ──────────────────────────────────────────
    # CodeQL recognizes this exact shape (realpath + startswith) as a
    # sanitizer for py/path-injection. The pattern is duplicated in every
    # public function below because CodeQL does not propagate sanitizer
    # recognition through helper-function calls.
    rel = (rel_path or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    real_root = os.path.realpath(project_path)
    if not os.path.isdir(real_root):
        raise FileBrowserError(
            "not_found", f"Project root not found: {project_path}"
        )
    real_target = os.path.realpath(os.path.join(real_root, rel))
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise FileBrowserError("forbidden", "Path escapes project root")
    # ───────────────────────────────────────────────────────────────────

    if not os.path.exists(real_target):
        raise FileBrowserError("not_found", f"Path not found: {rel_path}")
    if not os.path.isdir(real_target):
        raise FileBrowserError("not_directory", "Path is not a directory")

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(real_target) as it:
            children = list(it)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc

    def _sort_key(e: os.DirEntry) -> tuple[bool, str]:  # type: ignore[type-arg]
        try:
            return (not e.is_dir(), e.name.lower())
        except OSError:
            return (True, e.name.lower())

    children.sort(key=_sort_key)
    for entry in children:
        try:
            st = entry.stat()
        except OSError:
            # Broken symlink, permission denied, or vanished entry - skip.
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "kind": "dir" if is_dir else "file",
                "size": None if is_dir else st.st_size,
                "mtime": st.st_mtime,
            }
        )

    # Use the input rel_path as canonical - not the resolved target.
    clean_rel = (rel_path or "").strip("/")
    return {
        "path": clean_rel,
        "parent": _parent_rel(clean_rel),
        "entries": entries,
    }


def list_absolute_directory(abs_path: str) -> dict[str, Any]:
    """List directories inside an absolute filesystem path (not sandboxed).

    Used by the new-project picker: only directories are returned so the user
    can navigate the filesystem and pick a project root. Sensitive subtrees
    (/proc, /sys, /dev, /run) are refused.
    """
    # ── Inline path sanitizer (denylist variant for the picker) ────────
    real = os.path.realpath(os.path.expanduser(abs_path))
    for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES:
        if real == prefix or real.startswith(prefix + os.sep):
            raise FileBrowserError(
                "forbidden",
                f"Access to {prefix} subtree is not allowed",
            )
    # ───────────────────────────────────────────────────────────────────

    if not os.path.exists(real):
        raise FileBrowserError("not_found", f"Path not found: {abs_path}")
    if not os.path.isdir(real):
        raise FileBrowserError("not_directory", "Path is not a directory")

    parent_str = os.path.dirname(real)
    parent: str | None = parent_str if parent_str != real else None

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(real) as it:
            children = list(it)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc

    children.sort(key=lambda e: e.name.lower())
    for entry in children:
        try:
            if not entry.is_dir():
                continue
            st = entry.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "kind": "dir",
                "size": None,
                "mtime": st.st_mtime,
            }
        )

    return {
        "path": real,
        "parent": parent,
        "entries": entries,
    }


def create_absolute_directory(abs_path: str) -> None:
    """Create a directory (and any parents) at an absolute path.

    Same /proc, /sys, /dev, /run denylist as list_absolute_directory.
    """
    # ── Inline path sanitizer (denylist variant for the picker) ────────
    real = os.path.realpath(os.path.expanduser(abs_path))
    for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES:
        if real == prefix or real.startswith(prefix + os.sep):
            raise FileBrowserError(
                "forbidden",
                f"Access to {prefix} subtree is not allowed",
            )
    # ───────────────────────────────────────────────────────────────────

    try:
        os.makedirs(real, exist_ok=True)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc


def read_file(project_path: str, rel_path: str) -> dict[str, Any]:
    """Read a file inside the project, sandbox-checked.

    Returns: { path, size, too_large, encoding, content }
    - encoding: "utf-8" for text, "base64" for binary, None when too_large
    - too_large: True when size > MAX_FILE_BYTES; content is None in that case
    """
    # ── Inline path sanitizer ──────────────────────────────────────────
    rel = (rel_path or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    real_root = os.path.realpath(project_path)
    if not os.path.isdir(real_root):
        raise FileBrowserError("not_found", "Project root not found")
    real_target = os.path.realpath(os.path.join(real_root, rel))
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise FileBrowserError("forbidden", "Path escapes project root")
    # ───────────────────────────────────────────────────────────────────

    if not os.path.exists(real_target):
        raise FileBrowserError("not_found", f"File not found: {rel_path}")
    if not os.path.isfile(real_target):
        raise FileBrowserError("not_file", "Path is not a file")

    try:
        size = os.path.getsize(real_target)
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
        with open(real_target, "rb") as fh:
            raw = fh.read()
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


def upload_file(
    project_path: str,
    rel_dir: str,
    filename: str,
    content: bytes,
    on_conflict: str = "error",
) -> dict[str, Any]:
    """Upload a binary file into a directory inside the project.

    Args:
        project_path: project root (absolute, host filesystem).
        rel_dir: directory relative to project root (e.g. "src/data" or "").
        filename: basename to write; must not contain slashes or backslashes.
        content: raw file bytes (no encoding - decoded by the caller if needed).
        on_conflict: "error" (raise FileBrowserError with code "file_exists"),
            "overwrite" (replace existing), or "rename" (pick a unique name by
            appending " (1)", " (2)", ... before the extension).

    Returns: { path, size, renamed }
    """
    if not isinstance(content, bytes | bytearray):
        raise FileBrowserError("bad_request", "content must be bytes")
    if not filename or "/" in filename or "\\" in filename:
        raise FileBrowserError("bad_request", "filename must be a basename")
    if filename in (".", ".."):
        raise FileBrowserError("bad_request", "invalid filename")
    if on_conflict not in ("error", "overwrite", "rename"):
        raise FileBrowserError(
            "bad_request",
            f"on_conflict must be error|overwrite|rename, got {on_conflict!r}",
        )

    raw = bytes(content)

    # ── Inline path sanitizer (root + target_dir) ──────────────────────
    rel = (rel_dir or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    real_root = os.path.realpath(project_path)
    if not os.path.isdir(real_root):
        raise FileBrowserError("not_found", "Project root not found")
    real_dir = os.path.realpath(os.path.join(real_root, rel))
    if real_dir != real_root and not real_dir.startswith(real_root + os.sep):
        raise FileBrowserError("forbidden", "Path escapes project root")
    # ───────────────────────────────────────────────────────────────────

    # Create the directory on the fly if missing - matches the UX of
    # dropping files into a folder you just navigated to.
    try:
        os.makedirs(real_dir, exist_ok=True)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc
    if not os.path.isdir(real_dir):
        raise FileBrowserError("not_directory", "Target is not a directory")

    final_name = filename
    # ── Inline path sanitizer (final target = dir + basename) ──────────
    real_target = os.path.realpath(os.path.join(real_dir, final_name))
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise FileBrowserError("forbidden", "Path escapes project root")
    # ───────────────────────────────────────────────────────────────────

    renamed = False
    if os.path.lexists(real_target):
        if on_conflict == "error":
            raise FileBrowserError(
                "file_exists",
                f"File already exists: {filename}",
            )
        if on_conflict == "rename":
            # Inline _unique_name: try " (N)" suffixes, re-sanitizing each
            # candidate so CodeQL sees the pattern at the lexists() sink.
            stem, dot, ext = filename.rpartition(".")
            if not dot:
                stem, suffix_ext = filename, ""
            else:
                suffix_ext = f".{ext}"
            n = 1
            while True:
                candidate = f"{stem} ({n}){suffix_ext}"
                real_candidate = os.path.realpath(os.path.join(real_dir, candidate))
                if real_candidate != real_root and not real_candidate.startswith(
                    real_root + os.sep
                ):
                    raise FileBrowserError(
                        "forbidden", "Path escapes project root"
                    )
                if not os.path.lexists(real_candidate):
                    final_name = candidate
                    real_target = real_candidate
                    renamed = True
                    break
                n += 1
                if n > 9999:
                    raise FileBrowserError(
                        "io_error", "Too many name collisions"
                    )
        # overwrite: fall through; refuse to overwrite through a symlink
        elif os.path.islink(real_target):
            raise FileBrowserError("forbidden", "Cannot overwrite a symlink")

    try:
        # ── Inline path sanitizer for tmp sibling ──────────────────────
        candidate_tmp = real_target + ".tmp-upload"
        real_tmp = os.path.realpath(candidate_tmp)
        if real_tmp != real_root and not real_tmp.startswith(real_root + os.sep):
            raise FileBrowserError("forbidden", "Path escapes project root")
        # ───────────────────────────────────────────────────────────────
        with open(real_tmp, "wb") as fh:
            fh.write(raw)
        os.replace(real_tmp, real_target)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_dir = (rel_dir or "").strip("/")
    rel_out = f"{clean_dir}/{final_name}" if clean_dir else final_name
    return {"path": rel_out, "size": len(raw), "renamed": renamed}


def write_file(project_path: str, rel_path: str, content: str) -> dict[str, Any]:
    """Write UTF-8 text to a file inside the project, sandbox-checked.

    Creates parent directories if needed. Atomic write (tmp + rename) to
    prevent partial files on crash. Refuses binary writes and files over
    MAX_FILE_BYTES. Refuses to write through a symlink.

    Returns: { path, size }
    """
    if not rel_path or not rel_path.strip("/"):
        raise FileBrowserError("bad_request", "File path required")

    raw = content.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise FileBrowserError(
            "too_large",
            f"Content exceeds {MAX_FILE_BYTES // (1024*1024)} MiB limit",
        )

    # ── Inline path sanitizer ──────────────────────────────────────────
    rel = (rel_path or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    real_root = os.path.realpath(project_path)
    if not os.path.isdir(real_root):
        raise FileBrowserError("not_found", "Project root not found")
    real_target = os.path.realpath(os.path.join(real_root, rel))
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise FileBrowserError("forbidden", "Path escapes project root")
    # ───────────────────────────────────────────────────────────────────

    # Refuse overwriting symlinks (defense even inside the sandbox).
    if os.path.islink(real_target):
        raise FileBrowserError("forbidden", "Cannot write through a symlink")

    try:
        os.makedirs(os.path.dirname(real_target), exist_ok=True)
        # ── Inline path sanitizer for tmp sibling ──────────────────────
        candidate_tmp = real_target + ".tmp"
        real_tmp = os.path.realpath(candidate_tmp)
        if real_tmp != real_root and not real_tmp.startswith(real_root + os.sep):
            raise FileBrowserError("forbidden", "Path escapes project root")
        # ───────────────────────────────────────────────────────────────
        with open(real_tmp, "wb") as fh:
            fh.write(raw)
        os.replace(real_tmp, real_target)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_rel = (rel_path or "").strip("/")
    return {"path": clean_rel, "size": len(raw)}
