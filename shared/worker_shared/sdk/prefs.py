"""Shared session-preference command handlers.

The ``set_model`` / ``set_effort`` WS routes are byte-identical across every
worker type (claude, copilot, windows): apply the change to the live
SessionManager and echo a ``system`` message back so the frontend can update
its model/effort badge. Centralising them here keeps the acknowledgement
message shape consistent across workers - a change to how the frontend
consumes ``model_changed`` / ``effort_changed`` only has to land in one place
instead of being copy-pasted into each worker's handler.
"""

from __future__ import annotations

from typing import Any


async def apply_set_model(payload: dict[str, Any], sessions: Any, send: Any) -> None:
    """Apply a ``set_model`` request and emit the ``model_changed`` ack."""
    model = payload.get("model") or None
    await sessions.set_model(model)
    await send({
        "type": "system",
        "payload": {"subtype": "model_changed", "data": {"model": model}},
    })


async def apply_set_effort(payload: dict[str, Any], sessions: Any, send: Any) -> None:
    """Apply a ``set_effort`` request and emit the ``effort_changed`` ack."""
    effort = payload.get("effort") or None
    await sessions.set_effort(effort)
    await send({
        "type": "system",
        "payload": {"subtype": "effort_changed", "data": {"effort": effort}},
    })
