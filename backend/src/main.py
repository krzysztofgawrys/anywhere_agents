"""FastAPI application entry point."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from fastapi import Body
from src.auth.cf_access import verify_cf_access_token
from src.db import db
from src.projects.scanner import scan_and_register
from src.push.manager import push_manager
from src.sdk.registry import registry
from src.ws.handler import handle_websocket, manager

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Init DB + scan ~/.claude/projects/ on startup."""
    await db.init()
    count = await scan_and_register(db)
    logger.info("startup_complete", projects_registered=count)
    yield
    # Graceful shutdown: stop all sessions that are still parked in the
    # registry so the SDK subprocesses don't become orphans.
    await registry.stop_all()


app = FastAPI(title="Claude Web", version="0.1.0", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/api/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "connections": manager.active_connections})


@app.get("/api/push/vapid-public-key")
async def vapid_public_key() -> JSONResponse:
    """Return the VAPID public key so the browser can subscribe."""
    return JSONResponse({"publicKey": push_manager.vapid_public_key_b64()})


@app.post("/api/push/subscribe")
async def push_subscribe(subscription: dict = Body(...)) -> JSONResponse:
    """Register or refresh a browser push subscription."""
    push_manager.add_subscription(subscription)
    return JSONResponse({"ok": True})


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: dict = Body(...)) -> JSONResponse:
    """Remove a push subscription (browser unsubscribed)."""
    push_manager.remove_subscription(body.get("endpoint", ""))
    return JSONResponse({"ok": True})


@app.get("/api/push/test")
async def push_test() -> JSONResponse:
    """Dev-only: fire a test push to all subscribers."""
    subs = len(push_manager._subscriptions)  # type: ignore[attr-defined]
    await push_manager.notify_all("Claude finished", "Task completed — tap to view.")
    return JSONResponse({"subscribers": subs})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint with CF Access JWT verification on handshake."""
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

    await handle_websocket(websocket, connection_id, device_label=device_label)


def _device_label_from_headers(websocket: WebSocket, claims: dict[str, Any]) -> str:
    """Build a short human-readable device label for the lock manager."""
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
#
# index.html must NEVER be cached (CF, browser, anything): it carries the
# pointers to the hashed asset filenames. Hashed assets are content-addressed
# and safe to cache forever.
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
            # PWA manifest, service worker, and icons must never be cached —
            # Chrome uses these to build the WebAPK; stale copies prevent icon
            # updates from propagating to the Android home screen.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


if STATIC_DIR.exists():
    # Explicit /index.html route so refreshing on a deep URL still works and
    # never serves stale HTML.
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
