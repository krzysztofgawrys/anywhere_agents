"""Per-session prompt queue + client_msg_id dedup window.

Before this module a prompt that arrived while the agent was mid-turn was
answered with `error: busy` and thrown away. That is the single most common
way a user message got lost: auto-compaction and knowledge consolidation both
flip the session busy for minutes without the frontend necessarily knowing, so
the browser sent a perfectly normal prompt into a rejection it then rolled
back. Queueing instead of rejecting matches what the Claude Code CLI does.

Why module-level state and not fields on SessionManager: the manager is built
per WS connection, so a browser reconnect (mobile PWA backgrounded, laptop
asleep, network blip) constructs a fresh one. A queue living there would be
discarded in exactly the situation it exists for - the client was already told
"queued" and stopped retrying, so the prompt would vanish again. Keyed by
session_id here, both the backlog and the dedup window survive reconnects and
the registry park/reattach cycle. Only a worker process restart clears them.

The dedup window is what makes client retries safe: the browser resends any
prompt it has no ack for, so the same client_msg_id can legitimately arrive
twice (its ack died with the socket). The second copy is acked, not run.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# How many client_msg_ids to remember for dedup. The browser only retries
# prompts still in its own outbox (a handful at most), so this is generous.
_MAX_SEEN_IDS = 500

# Cap the per-session backlog so a retry storm or a runaway client can't grow
# it without bound. Hitting this returns "rejected" and the browser surfaces
# the text back to the composer instead of pretending it was delivered.
MAX_QUEUE_DEPTH = 50

# Submission outcomes, reported back to the browser as `prompt_ack.status`.
ACCEPTED = "accepted"
QUEUED = "queued"
DUPLICATE = "duplicate"
REJECTED = "rejected"


@dataclass
class QueuedPrompt:
    """One user prompt waiting for the session to go idle."""

    client_msg_id: str | None
    text: str
    auto_approve_once: bool = False
    images: list[dict[str, str]] = field(default_factory=list)
    stream: bool = False


class _SessionQueue:
    def __init__(self) -> None:
        self.items: deque[QueuedPrompt] = deque()
        self.pump: asyncio.Task[None] | None = None
        # Serializes the "is the session free? then dispatch" decision between
        # submit() and the pump. Without it both can observe an idle session
        # and dispatch, and the loser gets a spurious `busy` error.
        self.lock = asyncio.Lock()
        self.closed = False


_queues: dict[str, _SessionQueue] = {}
_seen: OrderedDict[str, None] = OrderedDict()


def mark_seen(client_msg_id: str) -> bool:
    """Record a client_msg_id. Returns False when it was already seen."""
    if client_msg_id in _seen:
        _seen.move_to_end(client_msg_id)
        return False
    _seen[client_msg_id] = None
    while len(_seen) > _MAX_SEEN_IDS:
        _seen.popitem(last=False)
    return True


def forget(client_msg_id: str) -> None:
    """Drop an id from the dedup window so a later retry is accepted.

    Used when we mark an id seen but then fail to take responsibility for it
    (queue full, no session) - otherwise the retry would be silently swallowed
    as a duplicate of a prompt nobody ever ran.
    """
    _seen.pop(client_msg_id, None)


def depth(session_id: str) -> int:
    q = _queues.get(session_id)
    return len(q.items) if q else 0


async def submit(session: Any, item: QueuedPrompt) -> str:
    """Dispatch `item` now, or queue it if the session is busy.

    Returns one of ACCEPTED / QUEUED / REJECTED.
    """
    q = _queues.setdefault(session.session_id, _SessionQueue())
    q.closed = False
    async with q.lock:
        if not q.items and not session.is_busy:
            await _dispatch(session, item)
            return ACCEPTED
        if len(q.items) >= MAX_QUEUE_DEPTH:
            logger.warning(
                "prompt_queue_full",
                session_id=session.session_id,
                depth=len(q.items),
            )
            return REJECTED
        q.items.append(item)
        logger.info(
            "prompt_queued",
            session_id=session.session_id,
            depth=len(q.items),
            client_msg_id=item.client_msg_id,
        )
    _ensure_pump(session, q)
    return QUEUED


def discard(session_id: str) -> None:
    """Drop a session's queue and stop its pump - the session is gone.

    Called when a session is stopped (registry TTL expiry, shutdown, lock
    takeover). Anything still queued is lost, so log loudly: the browser was
    told "queued" and will not retry on its own.
    """
    q = _queues.pop(session_id, None)
    if q is None:
        return
    q.closed = True
    if q.items:
        logger.warning(
            "prompt_queue_discarded",
            session_id=session_id,
            lost=len(q.items),
        )
    if q.pump is not None:
        q.pump.cancel()
        q.pump = None


def _ensure_pump(session: Any, q: _SessionQueue) -> None:
    if q.pump is not None and not q.pump.done():
        return
    q.pump = asyncio.create_task(_pump(session, q))


async def _dispatch(session: Any, item: QueuedPrompt) -> None:
    await session.send_prompt(
        item.text,
        auto_approve_once=item.auto_approve_once,
        images=item.images,
        stream=item.stream,
    )


async def _pump(session: Any, q: _SessionQueue) -> None:
    """Drain the backlog, one prompt per idle window.

    The pump outlives the SessionManager that started it (it holds the session
    object, not the connection), so a browser reconnect mid-backlog doesn't
    stall the queue.
    """
    sid = session.session_id
    try:
        while q.items and not q.closed:
            await session.wait_idle()
            if q.closed:
                return
            async with q.lock:
                # Re-check under the lock: knowledge consolidation (or another
                # dispatcher) can have flipped the session busy again between
                # wait_idle returning and us acquiring the lock.
                if session.is_busy:
                    continue
                if not q.items:
                    return
                item = q.items.popleft()
                try:
                    await _dispatch(session, item)
                except Exception as exc:
                    # Put it back and give up for now; the next submit() (or a
                    # reattach) restarts the pump rather than dropping work.
                    q.items.appendleft(item)
                    logger.warning(
                        "queued_prompt_dispatch_failed",
                        session_id=sid,
                        error=str(exc),
                    )
                    return
            logger.info(
                "queued_prompt_dispatched",
                session_id=sid,
                remaining=len(q.items),
                client_msg_id=item.client_msg_id,
            )
    except asyncio.CancelledError:
        return
    finally:
        if q.pump is asyncio.current_task():
            q.pump = None
