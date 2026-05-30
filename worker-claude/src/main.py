"""worker-claude entry point.

Two run modes selected by `WORKER_MODE` env var (mirrors worker-copilot):

  - "server" (default): classic mode. Worker runs a WS server; hub dials
    in via /ws on the worker's listen port (8002). Use when hub can reach
    the worker over LAN, VPN, Tailscale, VPC routing, etc.

  - "inbound": reverse mode. Worker does NOT serve /ws; instead it dials
    out to the hub at HUB_PUBLIC_URL/worker-register, identifying itself
    as WORKER_ID, and reacts to `open_data_channel` commands by dialing
    fresh data WS per browser session. Use when the worker sits in a
    closed network (VPC, behind NAT, corporate firewall) with only
    egress 443 to the hub available. No inbound ports required.

Note on /internal/upload (HTTP multipart file upload):
- In server mode the hub POSTs files here directly (`worker.url` derived
  to HTTP). In inbound mode the hub has no HTTP path to the worker and
  file upload via this route is unavailable. Browser-side upload will
  still appear to work for outbound workers; uploads targeting an
  inbound worker will fail at the hub's proxy layer. Future work: route
  uploads through the WS data channel for inbound workers too.

Inbound-mode env vars:
  HUB_PUBLIC_URL      https URL to the hub, e.g. https://hub.example.com
  WORKER_ID           must match an entry in workers.json with mode=inbound
  WORKER_LABEL        human-friendly label (default: WORKER_ID)
  WORKER_TYPE         agent SDK family (default: "claude")
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
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, status
from fastapi.responses import JSONResponse

from worker_shared.db import db
from worker_shared.files import FileBrowserError, upload_file
from worker_shared.sdk.registry import registry

from src.projects.scanner import scan_and_register
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
    # reachable hub URL, e.g. https://claude.example.com). HUB_URL on the
    # worker side is reserved for out-of-band push notifications (worker
    # has no such mechanism here as worker-claude doesn't ship push, but
    # the env split is kept symmetric with worker-copilot so config docs
    # are uniform). Fallback to HUB_URL for back-compat.
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
        worker_type=os.getenv("WORKER_TYPE", "claude"),
        worker_label=os.getenv("WORKER_LABEL", "") or worker_id,
        cf_client_id=os.getenv("CF_ACCESS_CLIENT_ID") or None,
        cf_client_secret=os.getenv("CF_ACCESS_CLIENT_SECRET") or None,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Init DB + scan projects on startup.

    In inbound mode also start the HubDialer so the worker reaches out
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


app = FastAPI(title="Claude Web Worker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": WORKER_MODE}


@app.post("/internal/upload")
async def internal_upload(
    file: UploadFile = File(...),  # noqa: B008
    project_path: str = Form(...),
    path: str = Form(""),
    filename: str | None = Form(None),
    on_conflict: str = Form("error"),
    x_worker_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Hub-only endpoint for file uploads.

    Auth: shared WORKER_SECRET (same as inbound WS auth). Hub holds the
    user-facing auth (CF Access) and proxies multipart bodies here.

    The frontend passes `project_path` (the project's absolute root on this
    worker's filesystem) directly - it has this value from `project.path` in
    the WS-served projects list. This avoids the per-WS project_id mapping
    that lives in `ProjectIndex` (which would not be reachable from a
    stateless HTTP handler without making it a global singleton).

    Note: in WORKER_MODE=inbound this endpoint stays mounted but the hub
    has no HTTP path to reach it (worker is dial-out only). File upload
    via the hub's proxy layer will fail for inbound workers until upload
    is migrated onto the WS data channel.

    Why HTTP and not WS:
    - WS frames are capped (uvicorn ws_max_size=16 MiB default). Base64
      inflation pushes a 13 MiB file past the limit, which is exactly what
      bit the user.
    - Mobile PWAs lose WS connections when backgrounded but `fetch` survives
      tab/app suspension reasonably well (browser keeps the request alive).
    - No protocol scaffolding needed - `UploadFile` streams from disk.
    """
    if WORKER_SECRET and x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    # Prefer the multipart filename hint from the field, fall back to the
    # UploadFile-provided filename (browser sets it from the File object).
    use_name = filename or file.filename or ""
    content = await file.read()
    logger.info(
        "upload_http_request",
        project_path=project_path,
        rel_dir=path,
        filename=use_name,
        on_conflict=on_conflict,
        size=len(content),
    )
    try:
        result = upload_file(project_path, path, use_name, content, on_conflict)
    except FileBrowserError as exc:
        logger.warning(
            "upload_http_failed",
            code=exc.code,
            message=exc.message,
            filename=use_name,
            rel_dir=path,
        )
        # 409 for conflicts so the hub/frontend can distinguish from other
        # client errors at the HTTP layer; other codes stay as 400.
        status_code = 409 if exc.code == "file_exists" else 400
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message},
        )

    logger.info(
        "upload_http_ok",
        project_path=project_path,
        path=result["path"],
        size=result["size"],
        renamed=result["renamed"],
    )
    return JSONResponse({
        "path": result["path"],
        "size": result["size"],
        "renamed": result["renamed"],
    })


@app.websocket("/ws")
async def worker_ws(websocket: WebSocket) -> None:
    """Hub-facing WS endpoint (server mode only).

    In inbound mode this endpoint stays mounted but actively refuses
    connections with 1008. Hub talks to this worker via data channels the
    worker dials out from HubDialer. The explicit refusal is a loud-fail
    safety net for the misconfig where someone keeps a stale workers.json
    entry pointing at this worker's URL after switching modes.
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


# Sanity check log - if mode is inbound, the /ws endpoint is mounted but
# will refuse connections (handled in worker_ws above). Emit a one-time
# hint so operators see immediately why this is intentional.
if WORKER_MODE == "inbound":
    logger.info(
        "inbound_mode_hint",
        message=(
            "WORKER_MODE=inbound active. The /ws server is mounted but will "
            "refuse connections. Hub talks to this worker via data channels "
            "the worker dials out from HubDialer. Disable any external port "
            "exposure for this container. /internal/upload is also mounted "
            "but unreachable from the hub in inbound mode (file upload via "
            "the proxy layer will not work for this worker)."
        ),
    )
