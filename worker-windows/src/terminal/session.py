"""Windows ConPTY terminal session using pywinpty.

Drop-in replacement for worker_shared.terminal.session.TerminalSession on
Windows. Runs PowerShell inside a ConPTY (Windows Pseudo Console) so the
hub terminal UI receives proper ANSI/xterm escape sequences.

Same public interface as the Unix PTY session:
  start(cols, rows)   - spawn PowerShell in a ConPTY
  stop()              - kill the process and clean up
  write(data: str)    - send raw input (key presses / text)
  resize(cols, rows)  - resize the ConPTY window

Sends WS messages:
  {"type": "terminal_output",  "payload": {"data": "<base64>"}}
  {"type": "terminal_closed",  "payload": {"exit_code": <int>}}
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger()

SendFn = Callable[[dict[str, Any]], Any]

# How long to sleep between non-blocking read polls when the PTY has no data.
_POLL_INTERVAL = 0.01


class TerminalSession:
    """Wraps a PowerShell process running inside a Windows ConPTY."""

    def __init__(self, send: SendFn, cwd: str) -> None:
        self._send = send
        self._cwd = cwd
        self._pty: Any = None
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, cols: int = 80, rows: int = 24) -> None:
        """Spawn PowerShell inside a ConPTY and begin streaming output."""
        import winpty  # pywinpty - Windows only

        self._pty = winpty.PTY(cols, rows)
        spawned = self._pty.spawn(
            "powershell.exe",
            cwd=self._cwd,
        )
        if not spawned:
            raise RuntimeError("Failed to spawn PowerShell in ConPTY")

        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("terminal_started", cwd=self._cwd, shell="powershell")

    async def stop(self) -> None:
        """Kill the PowerShell process and clean up the ConPTY."""
        if self._closed:
            return
        self._closed = True

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._pty is not None:
            try:
                self._pty.close()
            except Exception:
                pass
            self._pty = None

        logger.info("terminal_stopped", cwd=self._cwd)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, data: str) -> None:
        """Send raw input (key presses / typed text) to the PowerShell PTY."""
        if self._pty is not None and not self._closed:
            try:
                self._pty.write(data)
            except Exception:
                pass

    def resize(self, cols: int, rows: int) -> None:
        """Resize the ConPTY window."""
        if self._pty is not None and not self._closed:
            try:
                self._pty.set_size(cols, rows)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Poll the ConPTY for output and forward it to the WS client.

        pywinpty's read() is non-blocking by default. We poll in the event
        loop with a brief sleep when no data is available, and exit when
        the process dies.
        """
        loop = asyncio.get_running_loop()
        exit_code = 0
        try:
            while not self._closed:
                pty = self._pty
                if pty is None:
                    break

                # read() returns str; signature is read(blocking: bool).
                # Use blocking=False to avoid stalling the thread pool.
                data: str = await loop.run_in_executor(
                    None, lambda: pty.read(blocking=False)
                )

                if data:
                    encoded = base64.b64encode(
                        data.encode("utf-8", errors="replace")
                    ).decode("ascii")
                    await self._send({
                        "type": "terminal_output",
                        "payload": {"data": encoded},
                    })
                elif not pty.isalive():
                    # Process exited and buffer is drained
                    try:
                        status = pty.get_exitstatus()
                        exit_code = status if status is not None else 0
                    except Exception:
                        exit_code = 0
                    break
                else:
                    # No data yet - yield to the event loop briefly
                    await asyncio.sleep(_POLL_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("terminal_read_error", error=str(e))
        finally:
            if not self._closed:
                self._closed = True
                await self._send({
                    "type": "terminal_closed",
                    "payload": {"exit_code": exit_code},
                })
                logger.info("terminal_exited", cwd=self._cwd, exit_code=exit_code)
