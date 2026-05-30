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


def _resolve_within(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `root`, refusing any escape.

    Three guards stack:
      1. `..` segments are rejected in the raw input (cheap fail before
         hitting the filesystem).
      2. `root` is resolved to an absolute, symlink-free path.
      3. The candidate target is also resolved (`strict=False` so a
         not-yet-created file resolves to its eventual location). If the
         resolved candidate is not under the resolved root, the request
         is refused - this catches symlinks that would otherwise point at
         host-mounted secrets (~/.ssh, ~/.claude, ~/.gitconfig, ...).

    This is the sanitizer recognized by CodeQL's "Uncontrolled data used
    in path expression" rule; callers can trust the returned Path is
    inside `root`.
    """
    rel = (rel_path or "").lstrip("/")
    if ".." in rel.split("/"):
        raise FileBrowserError("forbidden", "Path traversal not allowed")
    root_resolved = root.resolve(strict=False)
    target = (root_resolved / rel).resolve(strict=False)
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        # Target resolved (after symlink expansion) to a path outside the
        # project root. This is the symlink-escape case described in the
        # module docstring.
        raise FileBrowserError(
            "forbidden", "Path escapes project root"
        ) from exc
    return target


def _assert_within(root: Path, target: Path) -> Path:
    """Re-assert that an already-built Path stays under `root`.

    `_resolve_within` is enough when callers feed it a single rel_path,
    but `upload_file` builds the final target as `dir / basename` after
    that initial check. The intermediate concat passes through CodeQL's
    taint flow as another "uncontrolled path" use even though the
    basename is validated separately. This helper closes the loop with
    a second containment check that matches the same sanitizer pattern
    as `_resolve_within`. Returns the resolved target.
    """
    root_resolved = root.resolve(strict=False)
    target_resolved = target.resolve(strict=False)
    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise FileBrowserError(
            "forbidden", "Path escapes project root"
        ) from exc
    return target_resolved


def _assert_safe_absolute(abs_path: Path) -> None:
    """Refuse absolute paths into kernel pseudo-filesystems or device nodes.

    Used by the new-project browser endpoints, which are intentionally
    *not* sandboxed to a project root (the user is picking one). We still
    don't want to expose /proc/<pid>/environ, /sys/kernel/* etc. via the
    listing/creating helpers. The resolved path is compared against a
    small denylist of well-known sensitive subtrees.
    """
    resolved_str = str(abs_path)
    for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES:
        if resolved_str == prefix or resolved_str.startswith(prefix + "/"):
            raise FileBrowserError(
                "forbidden",
                f"Access to {prefix} subtree is not allowed",
            )


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
            # Broken symlink, permission denied, or vanished entry - skip.
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
    target = Path(abs_path).expanduser().resolve(strict=False)
    _assert_safe_absolute(target)
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
    """Create a directory (and any parents) at an absolute path.

    Same /proc, /sys, /dev, /run denylist as list_absolute_directory.
    """
    target = Path(abs_path).expanduser().resolve(strict=False)
    _assert_safe_absolute(target)
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

    root = Path(project_path)
    if not root.exists() or not root.is_dir():
        raise FileBrowserError("not_found", "Project root not found")

    # Resolve the target directory (sandboxed, ".." rejected upstream).
    target_dir = _resolve_within(root, rel_dir or "")
    # Allow creating the directory on the fly if missing - matches the UX
    # of dropping files into a folder you just navigated to.
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc
    if not target_dir.is_dir():
        raise FileBrowserError("not_directory", "Target is not a directory")

    final_name = filename
    target = _assert_within(root, target_dir / final_name)
    renamed = False

    if target.exists() or target.is_symlink():
        if on_conflict == "error":
            raise FileBrowserError(
                "file_exists",
                f"File already exists: {filename}",
            )
        if on_conflict == "rename":
            final_name = _unique_name(target_dir, filename)
            target = _assert_within(root, target_dir / final_name)
            renamed = True
        # overwrite: fall through; refuse to overwrite through a symlink
        elif target.is_symlink():
            raise FileBrowserError("forbidden", "Cannot overwrite a symlink")

    try:
        tmp = target.with_suffix(target.suffix + ".tmp-upload")
        # tmp lives in the same already-sanitized directory; re-check
        # anyway so future edits can't accidentally introduce a write
        # outside root without tripping this guard.
        _assert_within(root, tmp)
        tmp.write_bytes(raw)
        tmp.replace(target)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_dir = (rel_dir or "").strip("/")
    rel_out = f"{clean_dir}/{final_name}" if clean_dir else final_name
    return {"path": rel_out, "size": len(raw), "renamed": renamed}


def _unique_name(directory: Path, filename: str) -> str:
    """Return a basename that does not yet exist in `directory`.

    Strategy: split on the LAST dot to preserve compound extensions reasonably
    (foo.tar.gz -> foo.tar (1).gz is wrong but rare; we accept that trade-off
    for simplicity). Increments " (N)" until a free slot is found.
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
        if not (directory / candidate).exists():
            return candidate
        n += 1
        if n > 9999:
            # Pathological case - give up and let the caller see io_error.
            raise FileBrowserError("io_error", "Too many name collisions")


def write_file(project_path: str, rel_path: str, content: str) -> dict[str, Any]:
    """Write UTF-8 text to a file inside the project, sandbox-checked.

    Creates parent directories if needed. Atomic write (tmp + rename) to
    prevent partial files on crash. Refuses binary writes and files over
    MAX_FILE_BYTES.

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

    root = Path(project_path)
    if not root.exists() or not root.is_dir():
        raise FileBrowserError("not_found", "Project root not found")
    target = _resolve_within(root, rel_path)

    # Refuse overwriting symlinks (prevent symlink-following attacks).
    unresolved = root / (rel_path or "").lstrip("/")
    if unresolved.is_symlink():
        raise FileBrowserError("forbidden", "Cannot write through a symlink")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
    except PermissionError as exc:
        raise FileBrowserError("forbidden", "Permission denied") from exc
    except OSError as exc:
        raise FileBrowserError("io_error", str(exc)) from exc

    clean_rel = (rel_path or "").strip("/")
    return {"path": clean_rel, "size": len(raw)}
