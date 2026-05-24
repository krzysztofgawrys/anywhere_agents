"""PTY-backed terminal session for a single WebSocket connection.

One TerminalSession wraps a bash process attached to a pseudo-terminal.
Output from the process is forwarded to the WS client as base64-encoded
``terminal_output`` messages; input from the client is written to the PTY
master fd.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import pty
import struct
import termios
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger()

SendFn = Callable[[dict[str, Any]], Any]


class TerminalSession:
    """Wraps a bash process running inside a PTY."""

    def __init__(self, send: SendFn, cwd: str) -> None:
        self._send = send
        self._cwd = cwd
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, cols: int = 80, rows: int = 24) -> None:
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._loop = asyncio.get_running_loop()

        self._set_winsize(cols, rows)

        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        }

        self._proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "--login",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=self._cwd,
            env=env,
            close_fds=True,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        # Non-blocking reader on master fd - fires in the event loop
        # without blocking a thread pool worker.
        self._loop.add_reader(master_fd, self._on_readable)
        logger.info("terminal_started", cwd=self._cwd, pid=self._proc.pid)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove_reader()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            await self._proc.wait()
        self._close_master_fd()
        logger.info("terminal_stopped", cwd=self._cwd)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, data: str) -> None:
        """Write raw input (key presses) from the client to the PTY."""
        if self._master_fd is not None and not self._closed:
            try:
                os.write(self._master_fd, data.encode("utf-8", errors="replace"))
            except OSError:
                pass

    def resize(self, cols: int, rows: int) -> None:
        self._set_winsize(cols, rows)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_readable(self) -> None:
        """Called by the event loop when data is available on the master fd."""
        try:
            data = os.read(self._master_fd, 65536)
            encoded = base64.b64encode(data).decode("ascii")
            asyncio.ensure_future(
                self._send({"type": "terminal_output", "payload": {"data": encoded}})
            )
        except OSError:
            # Process exited or fd closed
            self._remove_reader()
            asyncio.ensure_future(self._on_exit())

    async def _on_exit(self) -> None:
        if self._closed:
            return
        self._closed = True
        exit_code = await self._proc.wait() if self._proc else -1
        self._close_master_fd()
        await self._send({"type": "terminal_closed", "payload": {"exit_code": exit_code}})
        logger.info("terminal_exited", cwd=self._cwd, exit_code=exit_code)

    def _remove_reader(self) -> None:
        if self._loop and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except Exception:
                pass

    def _close_master_fd(self) -> None:
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def _set_winsize(self, cols: int, rows: int) -> None:
        if self._master_fd is not None:
            try:
                fcntl.ioctl(
                    self._master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
            except OSError:
                pass
