"""FastAPI application entry point."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, WebSocket, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.auth.cf_access import verify_cf_access_token
from src.db import db
from src.projects.scanner import scan_and_register
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


app = FastAPI(title="Claude Web", version="0.1.0", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/api/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "connections": manager.active_connections})


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
    logger.info("ws_auth_ok", email=claims.get("email"), connection_id=connection_id)

    await handle_websocket(websocket, connection_id)


# Serve static files (frontend build) — must be last
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
