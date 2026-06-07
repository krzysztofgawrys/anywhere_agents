"""worker-windows entry point.

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
  to HTTP). In inbound mode the hub has no HTTP path to the worker but
  upload still works: the hub routes the file through a fresh WS data
  channel (`type: "upload"` handled in ws/handler.py). Practical limit
  ~12 MiB raw per file (bounded by uvicorn's ws_max_size default after
  base64 + JSON envelope). Larger files get HTTP 413 from the hub.
  Outbound workers keep the streaming HTTP path with no such limit.

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

Windows-specific env vars:
  EXCEL_PORT          HTTPS port for the Excel MCP + add-in server (default: 3847)
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, status
from fastapi.responses import JSONResponse

from worker_shared.db import db
from worker_shared.files import FileBrowserError, upload_file
from worker_shared.projects.service import get_project_by_path
from worker_shared.sdk.registry import registry

from src.modules.base import WindowsModule
from src.modules.excel.module import ExcelModule
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
EXCEL_PORT = int(os.getenv("EXCEL_PORT", "3847"))

# Module registry - populated during lifespan startup, read by session.py
# to collect MCP configs. Each entry is a started WindowsModule instance.
_active_modules: list[WindowsModule] = []

# Module classes to instantiate on startup. Add new modules here.
MODULES: list[tuple[type, dict[str, Any]]] = [
    (ExcelModule, {"port": EXCEL_PORT}),
]


def get_active_modules() -> list[WindowsModule]:
    """Return the list of started modules. Used by session.py to collect MCP configs."""
    return list(_active_modules)


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
    """Init DB + scan projects + start modules on startup.

    In inbound mode also start the HubDialer so the worker reaches out
    to the hub immediately after init.
    """
    await db.init()
    count = await scan_and_register(db)

    # Start Windows automation modules
    for module_cls, kwargs in MODULES:
        module = module_cls(**kwargs)
        try:
            await module.start()
            _active_modules.append(module)
            logger.info("module_started", module=module.name, port=module.port)
        except Exception as e:
            logger.error(
                "module_start_failed",
                module=module_cls.__name__,
                error=str(e),
                exc_info=True,
            )

    # Write MCP settings so Claude CLI discovers module tools on next session.
    # Also called lazily per-session in session.py, but writing eagerly at
    # startup ensures standalone CLI usage works without going through the hub.
    for module in _active_modules:
        if hasattr(module, "write_mcp_settings"):
            try:
                module.write_mcp_settings()
            except Exception as e:
                logger.warning(
                    "mcp_settings_write_failed",
                    module=module.name,
                    error=str(e),
                )

    logger.info(
        "worker_startup_complete",
        projects_registered=count,
        mode=WORKER_MODE,
        modules_active=len(_active_modules),
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
        # Stop modules in reverse order
        for module in reversed(_active_modules):
            try:
                await module.stop()
                logger.info("module_stopped", module=module.name)
            except Exception as e:
                logger.warning(
                    "module_stop_failed", module=module.name, error=str(e)
                )
        _active_modules.clear()


app = FastAPI(title="Claude Web Worker (Windows)", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": WORKER_MODE}


@app.get("/modules/health")
async def modules_health() -> JSONResponse:
    """Return health status for all active Windows automation modules."""
    results: dict[str, Any] = {}
    for module in _active_modules:
        try:
            results[module.name] = await module.health()
        except Exception as e:
            results[module.name] = {"status": "error", "error": str(e)}
    return JSONResponse({"modules": results})


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

    # Validate project_path against the projects database so that only
    # registered project roots are accepted. Use the DB-stored path
    # (trusted) rather than the caller-supplied string.
    project = await get_project_by_path(db, project_path)
    if not project:
        raise HTTPException(status_code=403, detail="unknown project path")
    safe_project_path: str = project["path"]

    # Prefer the multipart filename hint from the field, fall back to the
    # UploadFile-provided filename (browser sets it from the File object).
    use_name = filename or file.filename or ""
    content = await file.read()
    logger.info(
        "upload_http_request",
        project_path=safe_project_path,
        rel_dir=path,
        filename=use_name,
        on_conflict=on_conflict,
        size=len(content),
    )
    try:
        result = upload_file(safe_project_path, path, use_name, content, on_conflict)
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
