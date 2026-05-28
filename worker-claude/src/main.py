"""Worker entry point - WS server for hub connections."""

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Init DB + scan projects on startup."""
    await db.init()
    count = await scan_and_register(db)
    logger.info("worker_startup_complete", projects_registered=count)
    yield
    await registry.stop_all()


app = FastAPI(title="Claude Web Worker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
    """Hub-facing WS endpoint. Auth via shared secret."""
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
