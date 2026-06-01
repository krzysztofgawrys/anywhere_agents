#!/usr/bin/env python3
"""Diagnostic version of _copilot_login.py - v2 with terminal attr checks.

Usage:
  python3 _copilot_login_diag.py <binary> <copilot_home> <timeout>
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import termios
import time

import pexpect


def _emit(event: str, **kw: object) -> None:
    print(json.dumps({"event": event, **kw}), flush=True)


def _diag(msg: str) -> None:
    print(f"[DIAG] {msg}", file=sys.stderr, flush=True)


def _dump_tty_attrs(fd: int, label: str) -> None:
    """Log terminal attributes for the PTY."""
    try:
        attrs = termios.tcgetattr(fd)
        iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
        _diag(f"  TTY {label}: iflag={iflag:#x} oflag={oflag:#x} lflag={lflag:#x}")
        _diag(f"    ICRNL={bool(iflag & termios.ICRNL)} "
               f"INLCR={bool(iflag & termios.INLCR)} "
               f"IGNCR={bool(iflag & termios.IGNCR)}")
        _diag(f"    ECHO={bool(lflag & termios.ECHO)} "
               f"ICANON={bool(lflag & termios.ICANON)} "
               f"ISIG={bool(lflag & termios.ISIG)}")
    except Exception as e:
        _diag(f"  TTY {label}: error reading attrs: {e}")


def main() -> None:
    if len(sys.argv) != 4:
        _emit("error", message="usage: _copilot_login_diag.py <binary> <copilot_home> <timeout>")
        sys.exit(1)

    binary = sys.argv[1]
    copilot_home = sys.argv[2]
    timeout = int(sys.argv[3])

    os.environ["COPILOT_HOME"] = copilot_home
    os.environ["TERM"] = "xterm-256color"

    _diag(f"parent stdin isatty={sys.stdin.isatty()}")

    logbuf = io.StringIO()
    cmd = f"{binary} login --config-dir {copilot_home}"
    _diag(f"spawning: {cmd}")

    child = pexpect.spawn(cmd, encoding="utf-8", timeout=timeout)
    child.logfile_read = logbuf

    _dump_tty_attrs(child.child_fd, "after spawn")

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
                # Check terminal attrs right when prompt appears
                _dump_tty_attrs(child.child_fd, "at prompt")
                _diag(f"prompt before={child.before!r}")
                _diag(f"prompt after={child.after!r}")

                # Try multiple approaches:
                # 1. First, disable ICRNL so raw \r reaches the CLI
                try:
                    attrs = termios.tcgetattr(child.child_fd)
                    attrs[0] = attrs[0] & ~termios.ICRNL  # disable CR->NL
                    termios.tcsetattr(child.child_fd, termios.TCSANOW, attrs)
                    _diag("disabled ICRNL on PTY")
                    _dump_tty_attrs(child.child_fd, "after ICRNL disable")
                except Exception as e:
                    _diag(f"failed to modify PTY attrs: {e}")

                time.sleep(0.1)
                child.send("y\r")
                _diag("sent y+CR with ICRNL disabled")
                _emit("plaintext_answered")

            elif idx == 2:
                _diag(f"EOF: before={child.before!r}")
                break

            elif idx == 3:
                _emit("error", message="timeout waiting for CLI output")
                break

    except pexpect.exceptions.EOF:
        _diag("caught EOF exception")
    except pexpect.exceptions.TIMEOUT:
        _emit("error", message="pexpect timeout")
    except Exception as exc:
        _diag(f"caught exception: {exc}")
        _emit("error", message=str(exc))

    _diag("--- full pexpect output ---")
    _diag(logbuf.getvalue())
    _diag("--- end pexpect output ---")

    if child.isalive():
        try:
            child.expect(pexpect.EOF, timeout=10)
        except Exception:
            pass

    child.close()
    exit_code = child.exitstatus if child.exitstatus is not None else -1
    _diag(f"exit_code={exit_code} signal={child.signalstatus}")

    # List files AFTER login
    _diag("--- files AFTER login ---")
    if os.path.isdir(copilot_home):
        for root, dirs, files in os.walk(copilot_home):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                    _diag(f"  {fp} ({sz} bytes)")
                    if f.endswith(".json") and sz < 2000:
                        with open(fp) as fh:
                            content = fh.read()
                        redacted = re.sub(
                            r"(gho_|ghu_|ghp_|github_pat_)\w{4}\w+",
                            r"\1****",
                            content,
                        )
                        _diag(f"    content: {redacted[:500]}")
                except OSError as e:
                    _diag(f"  {fp} (error: {e})")

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

    _diag(f"token_saved={token_saved}")
    _emit("done", exit_code=exit_code, token_saved=token_saved)


if __name__ == "__main__":
    main()
