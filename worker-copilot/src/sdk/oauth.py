"""OAuth device flow via the bundled `copilot login` subprocess.

The actual pexpect session runs in a SEPARATE PROCESS (not a thread)
because pexpect uses pty.fork() internally, and os.fork() from a
non-main thread in Python breaks controlling-terminal setup
(TIOCSCTTY). The Copilot CLI needs a proper controlling terminal to
show its interactive plaintext-storage prompt and actually save the
token. See _copilot_login.py for the subprocess entry point.

This module provides the async API surface that the WS handler calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

_DEFAULT_TIMEOUT_S = 600.0

# In-flight subprocesses keyed by request_id for cancel support.
_active: dict[str, asyncio.subprocess.Process] = {}

# Path to the helper script (lives next to this file).
_LOGIN_SCRIPT = str(Path(__file__).with_name("_copilot_login.py"))
_LOGIN_DIAG_SCRIPT = str(Path(__file__).with_name("_copilot_login_diag.py"))


def _locate_binary() -> str:
    """Return absolute path to the `copilot` CLI binary.

    Priority:
      1. npm-installed CLI on PATH (>= 1.0.51, writes config.json)
      2. SDK-bundled binary (fallback, older - may not write config.json)
    """
    from shutil import which

    # Prefer the npm-installed CLI - it reliably persists tokens to
    # config.json after `copilot login` (the SDK-bundled v1.0.36 does not).
    found = which("copilot")
    if found:
        return found

    # Fallback: SDK-bundled binary
    try:
        import copilot as _copilot_pkg  # type: ignore[import-not-found]
        pkg_dir = Path(_copilot_pkg.__file__).resolve().parent
        candidate = pkg_dir / "bin" / "copilot"
        if candidate.is_file():
            return str(candidate)
    except Exception:
        pass
    return "copilot"


async def run_device_flow(
    send: SendFn,
    *,
    worker_id: str,
    agent_type: str = "copilot",
    config_dir: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> bool:
    """Drive `copilot login` end-to-end, surfacing device code over WS.

    Returns True on successful authentication, False otherwise.
    """
    copilot_home = (
        config_dir
        or os.environ.get("COPILOT_HOME")
        or str(Path.home() / ".copilot")
    )
    Path(copilot_home).mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())
    expires_at = int(time.time() + timeout)
    binary = _locate_binary()

    logger.info(
        "copilot_oauth_start",
        request_id=request_id,
        config_dir=copilot_home,
        binary=binary,
    )

    # Spawn _copilot_login.py as a real subprocess (not a thread).
    # It uses os.fork() + pty.openpty() + TIOCSCTTY to give the copilot
    # binary a proper controlling terminal.
    proc = await asyncio.create_subprocess_exec(
        "python3", _LOGIN_SCRIPT, binary, copilot_home, str(int(timeout)),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _active[request_id] = proc

    device_code_emitted = False
    success = False

    try:
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("copilot_oauth_non_json", line=line[:200])
                continue

            event = msg.get("event")

            if event == "device_code" and not device_code_emitted:
                device_code_emitted = True
                code = msg["code"]
                url = msg.get("url", "https://github.com/login/device")
                await send({
                    "type": "auth_needed",
                    "payload": {
                        "worker_id": worker_id,
                        "agent_type": agent_type,
                        "flow": "device_code",
                        "request_id": request_id,
                        "instructions": (
                            "Open the activation page on any device "
                            "with a browser, sign in to GitHub if "
                            "needed, and enter the 8-character code "
                            "shown below. The worker picks up the "
                            "token automatically once you authorize."
                        ),
                        "expires_at": expires_at,
                        "device_code": code,
                        "verification_url": url,
                        "verification_url_complete": url,
                    },
                })
                logger.info(
                    "copilot_oauth_device_code_emitted",
                    request_id=request_id,
                    device_code=code,
                    url=url,
                )

            elif event == "plaintext_answered":
                logger.info(
                    "copilot_oauth_plaintext_answered",
                    request_id=request_id,
                )

            elif event == "done":
                exit_code = msg.get("exit_code", -1)
                token_saved = msg.get("token_saved", False)
                logger.info(
                    "copilot_oauth_done",
                    request_id=request_id,
                    exit_code=exit_code,
                    token_saved=token_saved,
                )
                if exit_code == 0 and token_saved:
                    success = True

            elif event == "error":
                logger.warning(
                    "copilot_oauth_helper_error",
                    request_id=request_id,
                    message=msg.get("message", ""),
                )

    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        raise
    finally:
        _active.pop(request_id, None)

    # Wait for process to fully exit
    await proc.wait()

    # Capture and log diagnostic stderr output
    if proc.stderr is not None:
        stderr_data = await proc.stderr.read()
        if stderr_data:
            stderr_text = stderr_data.decode("utf-8", errors="replace")
            for line in stderr_text.splitlines():
                logger.debug("copilot_login_diag", line=line)

    if success:
        try:
            from src.sdk.client import reset_client
            await reset_client()
        except Exception as exc:
            logger.warning("copilot_oauth_reset_client_failed", error=str(exc))
        await send({
            "type": "auth_status",
            "payload": {
                "worker_id": worker_id,
                "request_id": request_id,
                "state": "completed",
            },
        })
        logger.info("copilot_oauth_success", request_id=request_id)
        return True

    # Failure path
    error_msg = "copilot login failed."
    if device_code_emitted:
        error_msg = (
            "Login succeeded on GitHub but the CLI did not save the "
            "token. Please try again or paste a GitHub PAT instead."
        )
    await send({
        "type": "auth_status",
        "payload": {
            "worker_id": worker_id,
            "request_id": request_id,
            "state": "failed",
            "error": error_msg,
        },
    })
    logger.warning("copilot_oauth_failed", request_id=request_id)
    return False


def cancel_device_flow(request_id: str) -> bool:
    """Kill the in-flight subprocess for this request."""
    proc = _active.get(request_id)
    if proc is None:
        return False
    if proc.returncode is None:
        proc.terminate()
    _active.pop(request_id, None)
    return True
