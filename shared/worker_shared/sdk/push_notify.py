"""Out-of-band push notification helper.

Worker-side helper that POSTs to `hub/api/internal/push` over plain HTTP
(authenticated with the shared WORKER_SECRET). This is the path that lets
the user get a phone notification when the agent finishes a turn, asks
for permission, or needs a free-text answer - even if the browser WS is
gone (PWA backgrounded on Android, tab swiped away, etc.).

Why a separate module: this used to live duplicated in worker-claude
session.py AND worker-copilot session.py with identical code. Any new
agent worker needs the same primitive, so it moved here.
"""

from __future__ import annotations

import os

import httpx
import structlog

logger = structlog.get_logger()


async def emit_push_notify(
    *,
    title: str,
    body: str,
    timeout: float = 5.0,
) -> None:
    """Deliver a push notification to the hub over HTTP.

    Best-effort: swallows all errors after logging them. Failing to
    notify must never disrupt the agent loop. No-op when HUB_URL or
    WORKER_SECRET env vars are unset (e.g. local dev without push).
    """
    hub_url = os.environ.get("HUB_URL", "").rstrip("/")
    secret = os.environ.get("WORKER_SECRET", "")
    if not hub_url or not secret:
        logger.debug("push_notify_skipped_no_hub_config")
        return
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{hub_url}/api/internal/push",
                json={"title": title, "body": body},
                headers={"X-Worker-Secret": secret},
            )
            if resp.status_code != 200:
                logger.warning(
                    "push_notify_http_non_200",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
    except Exception as e:
        logger.warning("push_notify_http_failed", error=str(e))
