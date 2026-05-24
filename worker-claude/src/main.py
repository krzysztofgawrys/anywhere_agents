"""Worker entry point - WS server for hub connections."""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, WebSocket, status

from src.db import db
from src.projects.scanner import scan_and_register
from src.sdk.registry import registry
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
