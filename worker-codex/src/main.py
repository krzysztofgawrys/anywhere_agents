"""worker-codex entry point.

Two run modes selected by `WORKER_MODE` env var:

  - "server" (default): classic mode. Worker runs a WS server; hub dials
    in via /ws on the worker's listen port (8003). Use this when the hub
    can reach the worker over LAN, VPN, Tailscale, VPC routing, etc.

  - "inbound": reverse mode. Worker does NOT serve /ws; instead it dials
    out to the hub at HUB_URL/worker-register, identifying itself as
    WORKER_ID, and reacts to `open_data_channel` commands by dialing
    fresh data WS per browser session. Use this when the worker sits in
    a closed network (VPC, behind NAT, corporate firewall) with only
    egress to the hub available. No inbound ports required on the worker.

Inbound-mode env vars:
  HUB_URL             https URL to the hub, e.g. https://hub.example.com
  WORKER_ID           must match an entry in workers.json with mode=inbound
  WORKER_LABEL        human-friendly label (default: WORKER_ID)
  WORKER_TYPE         agent SDK family (default: "codex")
  WORKER_SECRET       shared secret matching hub's WORKER_SECRET
  CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET   optional - sent as
                      CF-Access-Client-Id/Secret headers when the hub
                      sits behind Cloudflare Access with a service-token
                      policy on /worker-register and /worker-data.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, WebSocket, status

from worker_shared.db import db
from worker_shared.sdk.registry import registry

from src.projects.scanner import scan_and_register
from src.sdk.client import stop_client
from src.ws.dialer import HubDialer
from src.ws.handler import handle_websocket

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
)

logger = structlog.get_logger()

WORKER_SECRET = os.getenv("WORKER_SECRET", "")
WORKER_MODE = os.getenv("WORKER_MODE", "server").lower()


def _build_dialer() -> HubDialer:
    # HUB_PUBLIC_URL is for reverse-mode dial-out (must be the publicly
    # reachable hub URL, e.g. https://claude.example.com). Distinct from
    # HUB_URL which is the *internal* address worker-copilot uses for
    # out-of-band push notifications (e.g. http://hub:8001 over the docker
    # network). They can be the same in a single-machine deployment if you
    # truly want push to round-trip through the tunnel, but typically they
    # differ. Fallback to HUB_URL for back-compat with the rev<reverse mode
    # docs from before this split.
    hub_url = os.getenv("HUB_PUBLIC_URL", "").strip() or os.getenv("HUB_URL", "").strip()
    worker_id = os.getenv("WORKER_ID", "").strip()
    if not hub_url:
        raise RuntimeError(
            "WORKER_MODE=inbound requires HUB_PUBLIC_URL (publicly reachable hub URL)"
        )
    if not worker_id:
        raise RuntimeError("WORKER_MODE=inbound requires WORKER_ID")
    return HubDialer(
        hub_url=hub_url,
        worker_id=worker_id,
        worker_secret=WORKER_SECRET,
        worker_type=os.getenv("WORKER_TYPE", "codex"),
        worker_label=os.getenv("WORKER_LABEL", "") or worker_id,
        cf_client_id=os.getenv("CF_ACCESS_CLIENT_ID") or None,
        cf_client_secret=os.getenv("CF_ACCESS_CLIENT_SECRET") or None,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Init DB + scan projects on startup. Codex client starts lazily on
    first session (via src.sdk.client.get_client); stop_client is a
    no-op for openai-codex-sdk but is called for symmetry with copilot.

    In inbound mode we also start the HubDialer so the worker reaches out
    to the hub immediately after init.
    """
    await db.init()
    count = await scan_and_register(db)
    logger.info(
        "worker_startup_complete", projects_registered=count, mode=WORKER_MODE
    )

    dialer: HubDialer | None = None
    if WORKER_MODE == "inbound":
        dialer = _build_dialer()
        await dialer.start()
        logger.info("hub_dialer_started", worker_id=os.getenv("WORKER_ID"))

    try:
        yield
    finally:
        if dialer is not None:
            await dialer.stop()
        await registry.stop_all()
        await stop_client()


app = FastAPI(title="Agents Anywhere Worker (Codex)", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": WORKER_MODE}


@app.websocket("/ws")
async def worker_ws(websocket: WebSocket) -> None:
    """Hub-facing WS endpoint (server mode only).

    In inbound mode this endpoint is still mounted but should never be
    used - the hub talks to us through the data channels we dial out for
    it. Left in place so misconfigured deployments (worker started in
    inbound mode but hub still has a stale workers.json with the worker's
    URL) fail loudly with an explicit 1008 rather than silently.
    """
    if WORKER_MODE == "inbound":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("worker_ws_rejected_inbound_mode")
        return

    auth_header = websocket.headers.get("authorization", "")
    if WORKER_SECRET and (
        not auth_header.startswith("Bearer ")
        or auth_header[7:] != WORKER_SECRET
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("worker_ws_auth_rejected")
        return

    device_label = websocket.query_params.get("device_label", "unknown")
    connection_id = str(uuid.uuid4())
    logger.info(
        "worker_ws_connected",
        connection_id=connection_id,
        device_label=device_label,
    )

    await handle_websocket(websocket, connection_id, device_label=device_label)


__all__ = ["app"]


# Sanity check: if mode is inbound, the /ws endpoint is mounted but will
# refuse connections (handled in worker_ws above). Emit a one-time hint so
# operators see immediately why this is intentional.
if WORKER_MODE == "inbound":
    logger.info(
        "inbound_mode_hint",
        message=(
            "WORKER_MODE=inbound active. The /ws server is mounted but will "
            "refuse connections. Hub talks to this worker via data channels "
            "the worker dials out from HubDialer. Disable any external port "
            "exposure for this container."
        ),
    )
