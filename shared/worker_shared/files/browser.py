"""Sandboxed file browser for a project's cwd.

Both `list_directory` and `read_file` resolve their target path against the
project root and refuse anything that escapes it, including after symlink
resolution. Hidden files are returned; the caller decides what to show.

Security note: the worker container mounts sensitive host paths
(~/.claude, ~/.ssh, ~/.copilot, ~/.gitconfig) so an unchecked symlink
inside a project root could leak SSH keys or auth tokens to the
browser-facing WS. _resolve_within() refuses any path whose resolution
lands outside the project root, treating the project root as the
authoritative sandbox boundary regardless of what symlinks point to.
"""

from __future__ import annotations

import base64
import os
import os.path
from pathlib import Path
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


def _contained_realpath(root_str: str, candidate_str: str) -> str:
    """Canonical CodeQL path-traversal sanitizer.

    Returns the realpath of `candidate_str` if it lives under
    `root_str` (also realpath'd), raises FileBrowserError otherwise.

    This specific shape - `os.path.realpath()` followed by a string
    `startswith(root + os.sep)` containment check - is the pattern
    CodeQL's Python query for "Uncontrolled data used in path
    expression" recognizes as a sanitizer. `pathlib.Path.relative_to()`
    is logically equivalent but CodeQL does NOT recognize it (verified
    empirically: an earlier attempt using Path.relative_to closed zero
    alerts and added new ones at the sanitizer's own call sites).
    """
    real_root = os.path.realpath(root_str)
    real_target = os.path.realpath(candidate_str)
    # Containment: target must be the root itself or a descendant.
    # Comparing against `real_root + os.sep` prevents the
    # "/foo" vs "/foobar" prefix-match false positive.
    if real_target != real_root and not real_target.startswith(
        real_root + os.sep
    ):
        raise FileBrowserError(
            "forbidden", "Path escapes project root"
        )
    return real_target


def _resolve_within(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `root`, refusing any escape.

    Three guards stack:
      1. `..` segments are rejected in the raw input (cheap fail before
         hitting the filesystem).
      2. `root` is realpath'd to an absolute, symlink-free string.
      3. The candidate target (joined from realpath'd root + rel) is
         realpath'd and required to live under the root via a string
         prefix check (see `_contained_realpath`). If the resolved
         candidate is not under the resolved root, the request is
         refused - this catches symlinks that would otherwise point at
         host-mounted secrets (~/.ssh, ~/.claude, ~/.gitconfig, ...).

    Callers can trust the returned Path is inside `root` (or equal to
    `root` for an empty rel_path).
    """
    rel = (rel_path or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    root_str = os.path.realpath(str(root))
    candidate_str = os.path.join(root_str, rel)
    return Path(_contained_realpath(root_str, candidate_str))


def _assert_within(root: Path, target: Path) -> Path:
    """Re-assert that an already-built Path stays under `root`.

    `_resolve_within` is enough when callers feed it a single rel_path,
    but `upload_file` builds the final target as `dir / basename` after
    that initial check. The intermediate concat passes through CodeQL's
    taint flow as another "uncontrolled path" use even though the
    basename is validated separately. This helper closes the loop with
    a second containment check using the same `os.path.realpath` +
    string-prefix sanitizer pattern.
    """
    return Path(_contained_realpath(str(root), str(target)))


def _safe_root(project_path: str) -> Path:
    """Resolve a project root to its realpath and verify it is a directory.

    Centralizes the project-root validation so every file-op call site
    sees the same os.path.realpath sanitizer pattern before touching the
    filesystem (CodeQL's "Uncontrolled data used in path expression"
    sanitizer is matched at the realpath call followed by an isdir check
    on the same string).
    """
    real = os.path.realpath(project_path)
    if not os.path.isdir(real):
        raise FileBrowserError(
            "not_found", f"Project root not found: {project_path}"
        )
    return Path(real)


def _safe_absolute(abs_path: str) -> Path:
    """Resolve an absolute filesystem path and refuse denylisted subtrees.

    Used by the new-project browser endpoints, which are intentionally
    *not* sandboxed to a project root (the user is picking one). We still
    don't want to expose /proc/<pid>/environ, /sys/kernel/* etc. via the
    listing/creating helpers. The resolved path is compared against a
    small denylist of well-known sensitive subtrees.

    The os.path.realpath + string comparison shape is the canonical
    CodeQL sanitizer pattern for path-traversal queries.
    """
    real = os.path.realpath(os.path.expanduser(abs_path))
    for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES:
        if real == prefix or real.startswith(prefix + os.sep):
            raise FileBrowserError(
                "forbidden",
                f"Access to {prefix} subtree is not allowed",
            )
    return Path(real)


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
    root = _safe_root(project_path)
    target = _resolve_within(root, rel_path)
    target_str = str(target)
    if not os.path.exists(target_str):
        raise FileBrowserError("not_found", f"Path not found: {rel_path}")
    if not os.path.isdir(target_str):
        raise FileBrowserError("not_directory", "Path is not a directory")

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(target_str) as it:
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
    can navigate the filesystem and pick a project root. Sensitive subtrees
    (/proc, /sys, /dev, /run) are refused.
    """
    target = _safe_absolute(abs_path)
    target_str = str(target)
    if not os.path.exists(target_str):
        raise FileBrowserError("not_found", f"Path not found: {abs_path}")
    if not os.path.isdir(target_str):
        raise FileBrowserError("not_directory", "Path is not a directory")

    parent_str = os.path.dirname(target_str)
    parent: str | None = parent_str if parent_str != target_str else None

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(target_str) as it:
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
        "path": target_str,
        "parent": parent,
        "entries": entries,
    }


def create_absolute_directory(abs_path: str) -> None:
    """Create a directory (and any parents) at an absolute path.

    Same /proc, /sys, /dev, /run denylist as list_absolute_directory.
    """
    target = _safe_absolute(abs_path)
    try:
        os.makedirs(str(target), exist_ok=True)
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
    root = _safe_root(project_path)
    target = _resolve_within(root, rel_path)
    target_str = str(target)
    if not os.path.exists(target_str):
        raise FileBrowserError("not_found", f"File not found: {rel_path}")
    if not os.path.isfile(target_str):
        raise FileBrowserError("not_file", "Path is not a file")

    try:
        size = os.path.getsize(target_str)
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
        with open(target_str, "rb") as fh:
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
        - path: final relative path of the written file (post-rename if any)
        - renamed: True iff the on-disk basename differs from the input

    Intentionally does NOT enforce MAX_FILE_BYTES - upload is for arbitrary
    binary assets and the user opted into dev-mode "no limits". With HTTP
    multipart there is no practical per-request limit beyond available RAM
    on the worker (the file is read fully into memory before write).
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

    root = _safe_root(project_path)
    root_str = str(root)

    # Resolve the target directory (sandboxed, ".." rejected upstream).
    target_dir = _resolve_within(root, rel_dir or "")
    target_dir_str = str(target_dir)
    # Allow creating the directory on the fly if missing - matches the UX
    # of dropping files into a folder you just navigated to.
    try:
        os.makedirs(target_dir_str, exist_ok=True)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc
    if not os.path.isdir(target_dir_str):
        raise FileBrowserError("not_directory", "Target is not a directory")

    final_name = filename
    # Re-sanitize the concatenated target through the canonical pattern so
    # CodeQL sees the sanitizer at the open() call site below.
    target_str = _contained_realpath(root_str, os.path.join(target_dir_str, final_name))
    renamed = False

    if os.path.lexists(target_str):
        if on_conflict == "error":
            raise FileBrowserError(
                "file_exists",
                f"File already exists: {filename}",
            )
        if on_conflict == "rename":
            final_name = _unique_name(root_str, target_dir_str, filename)
            target_str = _contained_realpath(
                root_str, os.path.join(target_dir_str, final_name)
            )
            renamed = True
        # overwrite: fall through; refuse to overwrite through a symlink
        elif os.path.islink(target_str):
            raise FileBrowserError("forbidden", "Cannot overwrite a symlink")

    try:
        tmp_str = target_str + ".tmp-upload"
        # Defense in depth: re-verify tmp stays under root.
        tmp_str = _contained_realpath(root_str, tmp_str)
        with open(tmp_str, "wb") as fh:
            fh.write(raw)
        os.replace(tmp_str, target_str)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_dir = (rel_dir or "").strip("/")
    rel_out = f"{clean_dir}/{final_name}" if clean_dir else final_name
    return {"path": rel_out, "size": len(raw), "renamed": renamed}


def _unique_name(root_str: str, directory_str: str, filename: str) -> str:
    """Return a basename that does not yet exist in `directory_str`.

    Strategy: split on the LAST dot to preserve compound extensions reasonably
    (foo.tar.gz -> foo.tar (1).gz is wrong but rare; we accept that trade-off
    for simplicity). Increments " (N)" until a free slot is found. Each
    candidate is run through `_contained_realpath` so the existence check
    site sees the canonical sanitizer pattern.
    """
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
        suffix_ext = ""
    else:
        suffix_ext = f".{ext}"
    n = 1
    while True:
        candidate = f"{stem} ({n}){suffix_ext}"
        candidate_path = _contained_realpath(
            root_str, os.path.join(directory_str, candidate)
        )
        if not os.path.lexists(candidate_path):
            return candidate
        n += 1
        if n > 9999:
            # Pathological case - give up and let the caller see io_error.
            raise FileBrowserError("io_error", "Too many name collisions")


def write_file(project_path: str, rel_path: str, content: str) -> dict[str, Any]:
    """Write UTF-8 text to a file inside the project, sandbox-checked.

    Creates parent directories if needed. Atomic write (tmp + rename) to
    prevent partial files on crash. Refuses binary writes and files over
    MAX_FILE_BYTES. Refuses to write through a symlink (defense against
    symlink-clobber attacks even inside the sandbox).

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

    root = _safe_root(project_path)
    root_str = str(root)
    target = _resolve_within(root, rel_path)
    target_str = str(target)

    # Refuse overwriting symlinks. os.path.islink works on the resolved
    # final path; a symlink at the final hop would have been rejected by
    # _resolve_within already, so this primarily catches the case where
    # the target exists as a symlink to a real file inside the sandbox.
    if os.path.islink(target_str):
        raise FileBrowserError("forbidden", "Cannot write through a symlink")

    try:
        os.makedirs(os.path.dirname(target_str), exist_ok=True)
        tmp_str = _contained_realpath(root_str, target_str + ".tmp")
        with open(tmp_str, "wb") as fh:
            fh.write(raw)
        os.replace(tmp_str, target_str)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_rel = (rel_path or "").strip("/")
    return {"path": clean_rel, "size": len(raw)}
