#!/usr/bin/env python3
"""Standalone copilot-login helper, run as a subprocess.

Must run in its own process because it needs a controlling terminal
for the Copilot CLI's interactive plaintext-storage prompt.

When spawned by uvicorn (stdout=PIPE), the Python process has NO
controlling terminal. The fix: call setsid() + TIOCSCTTY to acquire
one BEFORE running pexpect. This matches the environment of an
interactive `docker exec -it` session.

Protocol (stdout, one JSON line per event):
  {"event":"device_code","code":"XXXX-XXXX","url":"https://..."}
  {"event":"plaintext_answered"}
  {"event":"done","exit_code":0,"token_saved":true}
  {"event":"error","message":"..."}

Usage:
  python3 _copilot_login.py <binary> <copilot_home> <timeout>
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import termios

import pexpect


def _emit(event: str, **kw: object) -> None:
    print(json.dumps({"event": event, **kw}), flush=True)


def _acquire_controlling_terminal() -> int | None:
    """Give this process a controlling terminal if it lacks one.

    Returns the master fd (kept open to prevent PTY cleanup) or None
    if the process already has a controlling terminal.
    """
    # Check if we already have one
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.close(fd)
        return None  # already have a controlling terminal
    except OSError:
        pass

    # Create a new session (required for TIOCSCTTY)
    try:
        os.setsid()
    except OSError:
        # Already a session leader - that's fine
        pass

    # Allocate a PTY and claim it as our controlling terminal
    master_fd, slave_fd = os.openpty()
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
    except OSError as e:
        print(
            json.dumps({"event": "error", "message": f"TIOCSCTTY failed: {e}"}),
            flush=True,
        )
        os.close(master_fd)
        os.close(slave_fd)
        return None

    os.close(slave_fd)  # We only need master to keep the PTY alive
    return master_fd


def main() -> None:
    if len(sys.argv) != 4:
        _emit("error", message="usage: _copilot_login.py <binary> <copilot_home> <timeout>")
        sys.exit(1)

    binary = sys.argv[1]
    copilot_home = sys.argv[2]
    timeout = int(sys.argv[3])

    os.environ["COPILOT_HOME"] = copilot_home
    os.environ["TERM"] = "xterm-256color"

    # Acquire a controlling terminal so pexpect's child PTY setup
    # matches an interactive session (docker exec -it).
    dummy_master = _acquire_controlling_terminal()

    child = pexpect.spawn(
        binary + " login --config-dir " + copilot_home,
        encoding="utf-8",
        timeout=timeout,
    )

    try:
        while True:
            idx = child.expect([
                r"([A-Z0-9]{4}-[A-Z0-9]{4})",
                r"plaintext config file[^\?]*\?",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ])

            if idx == 0:
                code = child.match.group(1)
                url = "https://github.com/login/device"
                before = child.before or ""
                url_m = re.search(r"(https?://\S+/login/device)", before)
                if url_m:
                    url = url_m.group(1).rstrip(".,")
                _emit("device_code", code=code, url=url)

            elif idx == 1:
                child.sendline("y")
                _emit("plaintext_answered")

            elif idx == 2:
                break

            elif idx == 3:
                _emit("error", message="timeout waiting for CLI output")
                break

    except pexpect.exceptions.EOF:
        pass
    except pexpect.exceptions.TIMEOUT:
        _emit("error", message="pexpect timeout")
    except Exception as exc:
        _emit("error", message=str(exc))

    child.close()
    exit_code = child.exitstatus if child.exitstatus is not None else -1

    # Check token
    cfg = os.path.join(copilot_home, "config.json")
    token_saved = False
    if os.path.isfile(cfg):
        try:
            with open(cfg) as f:
                content = f.read()
            token_saved = any(
                m in content for m in ("gho_", "ghu_", "ghp_", "github_pat_")
            )
        except OSError:
            pass

    _emit("done", exit_code=exit_code, token_saved=token_saved)

    # Cleanup
    if dummy_master is not None:
        os.close(dummy_master)


if __name__ == "__main__":
    main()
