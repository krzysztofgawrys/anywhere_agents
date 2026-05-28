"""Hub entry point - frontend proxy, auth, worker routing."""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from src.auth.cf_access import verify_cf_access_token
from src.push.manager import push_manager
from src.workers.registry import WorkerInfo, load_workers
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

# Loaded at startup from workers.json
workers: list[WorkerInfo] = []


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global workers
    workers = load_workers()
    if not workers:
        logger.error("no_workers_configured")
    logger.info("hub_startup", workers=[w.id for w in workers])
    yield
    logger.info("hub_shutdown")


app = FastAPI(title="Claude Web Hub", version="0.1.0", lifespan=lifespan)

# Prefer a volume-mounted static dir (for live dev rebuilds from worker-claude)
# over the baked-in copy from the Docker image build.
_STATIC_MOUNT = Path(__file__).parent / "static-mount"
_STATIC_BAKED = Path(__file__).parent / "static"
STATIC_DIR = (
    _STATIC_MOUNT
    if (_STATIC_MOUNT.exists() and any(_STATIC_MOUNT.iterdir()))
    else _STATIC_BAKED
)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/push/vapid-public-key")
async def vapid_public_key() -> JSONResponse:
    return JSONResponse({"publicKey": push_manager.vapid_public_key_b64()})


@app.post("/api/push/subscribe")
async def push_subscribe(subscription: dict = Body(...)) -> JSONResponse:
    push_manager.add_subscription(subscription)
    return JSONResponse({"ok": True})


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: dict = Body(...)) -> JSONResponse:
    push_manager.remove_subscription(body.get("endpoint", ""))
    return JSONResponse({"ok": True})


@app.post("/api/push/test")
async def push_test() -> JSONResponse:
    subs = len(push_manager._subscriptions)  # type: ignore[attr-defined]
    await push_manager.notify_all("Claude finished", "Task completed - tap to view.")
    return JSONResponse({"subscribers": subs})


@app.post("/api/internal/push")
async def internal_push(
    body: dict = Body(...),
    x_worker_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Worker-only endpoint for emitting push notifications.

    Required because the worker's WS-based push_notify path is only reachable
    while a frontend client is connected - i.e. exactly NOT when we need it
    most (PWA swiped away on Android, session running detached in the
    background). HTTP is independent of any client WS lifecycle: as long as
    the hub container is alive, the worker can deliver a push.

    Auth: shared secret matching the hub's WORKER_SECRET env var (same value
    the worker uses to authenticate inbound hub WS connections, just reused
    in the other direction).
    """
    expected = os.environ.get("WORKER_SECRET", "")
    if not expected or x_worker_secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    await push_manager.notify_all(
        body.get("title", "Claude finished"),
        body.get("body", ""),
    )
    return JSONResponse({"ok": True})


@app.post("/api/upload")
async def upload_proxy(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    worker_id: str = Form(...),
    project_path: str = Form(...),
    path: str = Form(""),
    filename: str | None = Form(None),
    on_conflict: str = Form("error"),
) -> JSONResponse:
    """Multipart file upload - CF Access auth, forwards to selected worker.

    Why not WebSocket (the original implementation): WS frames are capped at
    16 MiB by uvicorn (`ws_max_size`), and after base64 inflation the cap
    falls to ~12 MiB of raw content. A 13 MiB file silently failed for the
    user. HTTP multipart has no analogous frame limit. Mobile PWAs also
    survive backgrounding better with fetch than with WS.

    Auth path: same CF Access JWT as the WS endpoint, sent here as either
    the standard header or a cookie (CF Access sets both). Fail-closed in
    prod, dev-bypass otherwise (handled inside `verify_cf_access_token`).
    """
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get(
        "CF_Authorization"
    )
    claims = await verify_cf_access_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="unauthorized")

    workers = load_workers()
    worker = next((w for w in workers if w.id == worker_id), None)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"unknown worker {worker_id}")

    # Worker URL in workers.json is the WS URL ("ws://host:port/ws"). Derive
    # the corresponding HTTP base by swapping the scheme and stripping the
    # WS path.
    http_base = worker.url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    if http_base.endswith("/ws"):
        http_base = http_base[: -len("/ws")]
    upload_url = f"{http_base}/internal/upload"

    # Stream the file body to the worker via httpx multipart. `file.read()`
    # buffers in memory - acceptable for dev/single-user usage; for
    # multi-tenant production we'd switch to chunked streaming. The user
    # explicitly opted into "no limits" so this stays simple.
    content = await file.read()
    files = {
        "file": (
            filename or file.filename or "upload.bin",
            content,
            file.content_type or "application/octet-stream",
        ),
    }
    data = {
        "project_path": project_path,
        "path": path,
        "on_conflict": on_conflict,
    }
    if filename:
        data["filename"] = filename

    logger.info(
        "upload_proxy_forward",
        worker_id=worker_id,
        project_path=project_path,
        rel_dir=path,
        filename=data.get("filename") or file.filename,
        size=len(content),
        email=claims.get("email"),
    )

    # No client-side timeout cap: large files over slow connections need
    # the worker's full processing window. The hub itself runs on a single
    # node + LAN to the worker so the only real failure modes are worker
    # crash and the user closing the tab.
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            resp = await client.post(
                upload_url,
                files=files,
                data=data,
                headers={"X-Worker-Secret": worker.secret},
            )
        except httpx.HTTPError as exc:
            logger.warning("upload_proxy_worker_unreachable", worker_id=worker_id, error=str(exc))
            raise HTTPException(
                status_code=502, detail=f"worker {worker_id} unreachable"
            ) from exc

    # Pass the worker's status + JSON body through verbatim so the frontend
    # can distinguish file_exists (409) from other errors.
    try:
        body = resp.json()
    except ValueError:
        body = {"code": "bad_gateway", "message": resp.text[:200]}
    return JSONResponse(status_code=resp.status_code, content=body)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Frontend WS - CF Access auth, then proxy to worker."""
    token = websocket.headers.get("cf-access-jwt-assertion")
    claims = await verify_cf_access_token(token)

    if claims is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("ws_auth_rejected", client=websocket.client)
        return

    connection_id = str(uuid.uuid4())
    device_label = _device_label_from_headers(websocket, claims)
    logger.info(
        "ws_auth_ok",
        email=claims.get("email"),
        connection_id=connection_id,
        device=device_label,
    )

    # NOTE: handle_websocket reads workers.json fresh on every call so config
    # edits land without a hub restart. The module-level `workers` snapshot is
    # only used for the startup log line.
    await handle_websocket(
        websocket,
        connection_id,
        device_label=device_label,
    )


def _device_label_from_headers(websocket: WebSocket, claims: dict[str, Any]) -> str:
    email = claims.get("email") or "anon"
    user_agent = websocket.headers.get("user-agent", "")
    ua = user_agent.lower()
    if "iphone" in ua or "ipad" in ua:
        device = "iOS"
    elif "android" in ua:
        device = "Android"
    elif "mac os" in ua or "macintosh" in ua:
        device = "Mac"
    elif "linux" in ua:
        device = "Linux"
    elif "windows" in ua:
        device = "Windows"
    else:
        device = "Browser"
    return f"{email} @ {device}"


# Serve static files (frontend build) - must be last.
class NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if path.endswith("index.html") or path in ("", "/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif (
            path in ("manifest.json", "sw.js", "favicon.ico")
            or path.endswith(".webmanifest")
            or path.startswith("icon-")
        ):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


if STATIC_DIR.exists():
    @app.get("/")
    async def root(_request: Request) -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    app.mount("/", NoCacheStatic(directory=str(STATIC_DIR), html=True), name="static")
