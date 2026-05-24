"""Hub entry point — frontend proxy, auth, worker routing."""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import Body, FastAPI, Request, WebSocket, status
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
    await push_manager.notify_all("Claude finished", "Task completed — tap to view.")
    return JSONResponse({"subscribers": subs})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Frontend WS — CF Access auth, then proxy to worker."""
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

    await handle_websocket(
        websocket,
        connection_id,
        device_label=device_label,
        workers=workers,
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


# Serve static files (frontend build) — must be last.
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
