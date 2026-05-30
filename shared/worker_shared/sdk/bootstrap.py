"""Worker-side bootstrap auth orchestration.

When a worker's SDK init fails for lack of credentials, the worker
calls `request_credentials(...)` here. It:

1. Emits an `auth_needed` WS message addressed at the hub (which
   forwards / broadcasts it to the user's browser sidebar).
2. Awaits a matching `auth_provided` WS message from the same hub
   carrying the credential payload the user typed into the modal.
3. Persists the credentials via credentials_store and returns the
   blob to the caller for immediate use (e.g. setting
   ANTHROPIC_API_KEY before retrying SDK init).

Resolution path is keyed by a per-request UUID so multiple
concurrent bootstraps (different agents, different workers,
multiple WS sessions) don't cross wires.

The hub routes `auth_provided` back to the specific worker that
emitted `auth_needed`; this module just owns the futures.

See docs/bootstrap-auth-protocol.md for the full protocol shape.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from worker_shared.sdk.credentials_store import save_credentials

logger = structlog.get_logger()


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


# Pending bootstrap futures, keyed by request_id. Module-global so the
# WS message router (in the worker's handler.py) can resolve them when
# `auth_provided` arrives, without threading the future through every
# session object.
_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}


def resolve_auth(request_id: str, credentials: dict[str, Any]) -> bool:
    """Resolve a pending request_credentials() call.

    Called by the worker's WS handler when an `auth_provided` message
    arrives. Returns True if a matching pending request existed.
    """
    fut = _pending.get(request_id)
    if fut is None or fut.done():
        return False
    fut.set_result(credentials)
    return True


def cancel_auth(request_id: str, reason: str = "Cancelled") -> bool:
    """Reject a pending request_credentials() call.

    Called when the user clicks "Cancel" in the modal (browser emits
    `auth_cancel`). The pending future is marked failed; the worker's
    bootstrap call site sees BootstrapCancelled.
    """
    fut = _pending.get(request_id)
    if fut is None or fut.done():
        return False
    fut.set_exception(BootstrapCancelled(reason))
    return True


class BootstrapError(Exception):
    """Bootstrap couldn't complete (timeout, cancel, malformed reply)."""


class BootstrapCancelled(BootstrapError):
    """User dismissed the modal."""


class BootstrapTimeout(BootstrapError):
    """User didn't respond within the time window."""


async def request_credentials(
    send: SendFn,
    *,
    worker_id: str,
    agent_type: str,
    flow: str = "api_key",
    instructions: str = "",
    extras: dict[str, Any] | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Ask the user (via hub + browser) for credentials.

    Emits `auth_needed`, blocks on a Future the WS handler will
    resolve when `auth_provided` arrives. On timeout emits
    `auth_status state=expired` so the frontend banner clears.

    Returns the credentials dict the user supplied (shape depends on
    `flow` - e.g. `{"api_key": "sk-ant-..."}` for `flow=api_key`).
    Also persists the blob via credentials_store so subsequent
    container starts skip the prompt.

    `extras` carries flow-specific extra fields (device_code,
    verification_url, ...) that get merged into the auth_needed
    payload verbatim.
    """
    request_id = str(uuid.uuid4())
    expires_at = int(time.time() + timeout)

    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "agent_type": agent_type,
        "flow": flow,
        "request_id": request_id,
        "instructions": instructions,
        "expires_at": expires_at,
    }
    if extras:
        payload.update(extras)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending[request_id] = future

    await send({"type": "auth_needed", "payload": payload})
    logger.info(
        "bootstrap_requested",
        worker_id=worker_id,
        agent_type=agent_type,
        flow=flow,
        request_id=request_id,
        expires_at=expires_at,
    )

    try:
        credentials = await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError as exc:
        await _emit_status(
            send,
            worker_id=worker_id,
            request_id=request_id,
            state="expired",
            error="No response from user within the time window.",
        )
        raise BootstrapTimeout(
            f"No auth response for {worker_id} ({agent_type}) within {timeout}s"
        ) from exc
    except BootstrapCancelled:
        await _emit_status(
            send,
            worker_id=worker_id,
            request_id=request_id,
            state="failed",
            error="User cancelled the bootstrap.",
        )
        raise
    finally:
        _pending.pop(request_id, None)

    # Persist before announcing success so a crash between status emit
    # and disk write doesn't leave the worker in a "claims completed
    # but lost the key" state.
    save_credentials(agent_type=agent_type, flow=flow, data=credentials)

    await _emit_status(
        send,
        worker_id=worker_id,
        request_id=request_id,
        state="completed",
    )
    logger.info(
        "bootstrap_completed",
        worker_id=worker_id,
        agent_type=agent_type,
        flow=flow,
        request_id=request_id,
    )
    return credentials


async def _emit_status(
    send: SendFn,
    *,
    worker_id: str,
    request_id: str,
    state: str,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "request_id": request_id,
        "state": state,
    }
    if error is not None:
        payload["error"] = error
    try:
        await send({"type": "auth_status", "payload": payload})
    except Exception as exc:
        logger.warning(
            "bootstrap_status_emit_failed",
            worker_id=worker_id,
            state=state,
            error=str(exc),
        )
