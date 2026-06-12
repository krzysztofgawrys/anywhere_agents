"""Hub entry point - frontend proxy, auth, worker routing."""

import asyncio
import json
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
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.auth.cf_access import verify_cf_access_token
from src.push.manager import push_manager
from src.workers.inbound import inbound_registry
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


app = FastAPI(title="Agents Anywhere Hub", version="0.1.0", lifespan=lifespan)

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
    await push_manager.notify_all("Agent finished", "Task completed - tap to view.")
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
        body.get("title", "Agent finished"),
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

    content = await file.read()
    use_filename = filename or file.filename or "upload.bin"

    logger.info(
        "upload_proxy_forward",
        worker_id=worker_id,
        mode=worker.mode,
        project_path=project_path,
        rel_dir=path,
        filename=use_filename,
        size=len(content),
        email=claims.get("email"),
    )

    # ── Inbound (reverse-mode) worker: send the upload through a
    # short-lived data WS opened via the worker's control channel. The
    # worker is dial-out only so we have no HTTP path to it.
    if worker.mode == "inbound":
        return await _upload_via_inbound_ws(
            worker_id=worker_id,
            project_path=project_path,
            rel_path=path,
            filename=use_filename,
            content=content,
            on_conflict=on_conflict,
        )

    # ── Outbound worker: classic HTTP POST to /internal/upload.
    # Worker URL in workers.json is the WS URL ("ws://host:port/ws"). Derive
    # the corresponding HTTP base by swapping the scheme and stripping the
    # WS path.
    http_base = worker.url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    if http_base.endswith("/ws"):
        http_base = http_base[: -len("/ws")]
    upload_url = f"{http_base}/internal/upload"

    files = {
        "file": (
            use_filename,
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


# uvicorn's default ws_max_size is 16 MiB. After base64 encoding (~33%
# overhead) and the wrapping JSON envelope, the practical raw-bytes cap
# is around 12 MiB. We reject anything bigger before opening the WS so
# the frontend gets a clean 413 instead of an opaque close-frame.
_INBOUND_UPLOAD_MAX_BYTES = 12 * 1024 * 1024


async def _upload_via_inbound_ws(
    *,
    worker_id: str,
    project_path: str,
    rel_path: str,
    filename: str,
    content: bytes,
    on_conflict: str,
) -> JSONResponse:
    """Route an upload to an inbound (reverse-mode) worker via a fresh WS.

    Protocol:
      hub -> worker:  {type: "upload", payload: {project_path, path,
                                                 filename, content_b64,
                                                 on_conflict}}
      worker -> hub:  {type: "upload_response", payload: <upload result>}
                  OR  {type: "upload_error", payload: {code, message}}

    The hub opens a dedicated data WS via InboundRegistry.open_session()
    just for this one upload, then closes it. Browser-facing response
    shape matches the HTTP /internal/upload path so the frontend can't
    tell the difference between outbound and inbound workers.
    """
    import base64

    if len(content) > _INBOUND_UPLOAD_MAX_BYTES:
        logger.warning(
            "upload_inbound_too_large",
            worker_id=worker_id,
            size=len(content),
            limit=_INBOUND_UPLOAD_MAX_BYTES,
        )
        raise HTTPException(
            status_code=413,
            detail=(
                f"Upload too large for inbound worker (max "
                f"{_INBOUND_UPLOAD_MAX_BYTES} bytes raw). The worker has "
                "no inbound HTTP port so the file has to fit in a single "
                "WebSocket frame."
            ),
        )

    try:
        ws = await inbound_registry.open_session(worker_id, timeout=10.0)
    except LookupError as exc:
        logger.warning("upload_inbound_no_control", worker_id=worker_id, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail=f"worker {worker_id} not registered (control channel down)",
        ) from exc
    except TimeoutError as exc:
        logger.warning("upload_inbound_session_timeout", worker_id=worker_id)
        raise HTTPException(
            status_code=504,
            detail=f"worker {worker_id} did not open data channel in time",
        ) from exc

    content_b64 = base64.b64encode(content).decode("ascii")
    try:
        await ws.send_text(json.dumps({
            "type": "upload",
            "payload": {
                "project_path": project_path,
                "path": rel_path,
                "filename": filename,
                "content_b64": content_b64,
                "on_conflict": on_conflict,
            },
        }))
        # Wait for the worker's response. Generous timeout because the
        # worker is reading + writing potentially-large files; we already
        # capped the size so this isn't unbounded.
        raw = await asyncio.wait_for(ws.receive_text(), timeout=60.0)
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.warning("upload_inbound_ws_dropped", worker_id=worker_id, error=str(exc))
        try:
            await ws.close()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail="worker WS dropped mid-upload") from exc
    except TimeoutError as exc:
        try:
            await ws.close()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail="worker did not respond to upload") from exc

    try:
        await ws.close()
    except Exception:
        pass

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502, detail="worker sent non-JSON upload response"
        ) from exc

    msg_type = msg.get("type")
    payload = msg.get("payload") or {}

    if msg_type == "upload_response":
        return JSONResponse(content=payload)

    if msg_type == "upload_error":
        # Mirror the HTTP path: 409 for file_exists, 400 for everything
        # else. The frontend already knows how to render both.
        status_code = 409 if payload.get("code") == "file_exists" else 400
        return JSONResponse(status_code=status_code, content=payload)

    logger.warning(
        "upload_inbound_unexpected_msg",
        worker_id=worker_id,
        msg_type=msg_type,
    )
    raise HTTPException(
        status_code=502,
        detail=f"unexpected worker response: type={msg_type}",
    )


def _worker_http_base(worker: WorkerInfo) -> str:
    """Derive a worker's HTTP base URL from its WS URL in workers.json."""
    http_base = worker.url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    if http_base.endswith("/ws"):
        http_base = http_base[: -len("/ws")]
    return http_base


async def _auth_download_request(request: Request) -> dict[str, Any]:
    """CF Access gate shared by /api/download and /api/zip.

    Browser-triggered downloads navigate (anchor click) rather than fetch, so
    the JWT arrives as the CF_Authorization cookie; we also accept the standard
    header for parity with /api/upload. Returns the claims or raises 401.
    """
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get(
        "CF_Authorization"
    )
    claims = await verify_cf_access_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return claims


async def _stream_from_worker(
    method: str,
    url: str,
    *,
    worker: WorkerInfo,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> StreamingResponse:
    """Proxy a download/zip response from an outbound worker back to the client.

    Streams the body so large files/zips never buffer fully in the hub. The
    worker's Content-Disposition + Content-Type are forwarded verbatim. On a
    non-200 the (small JSON error) body is read eagerly and returned as-is.
    """
    client = httpx.AsyncClient(timeout=None)
    req = client.build_request(
        method,
        url,
        params=params,
        json=json_body,
        headers={"X-Worker-Secret": worker.secret},
    )
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("download_proxy_worker_unreachable", worker_id=worker.id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"worker {worker.id} unreachable") from exc

    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        try:
            content = json.loads(body)
        except ValueError:
            content = {"code": "bad_gateway", "message": body.decode("utf-8", "replace")[:200]}
        return JSONResponse(status_code=resp.status_code, content=content)  # type: ignore[return-value]

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {}
    if "content-disposition" in resp.headers:
        headers["Content-Disposition"] = resp.headers["content-disposition"]
    if "content-length" in resp.headers:
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(
        body_iter(),
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


@app.get("/api/download")
async def download_proxy(
    request: Request,
    worker_id: str,
    project_path: str,
    path: str,
) -> StreamingResponse:
    """Stream a single project file from a worker for browser download.

    CF Access auth (cookie or header), then proxy to the worker's
    /internal/download. Inbound (reverse-mode) workers have no hub→worker HTTP
    path so they are unsupported here for now.
    """
    await _auth_download_request(request)
    worker = next((w for w in load_workers() if w.id == worker_id), None)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"unknown worker {worker_id}")
    if worker.mode == "inbound":
        raise HTTPException(status_code=501, detail="download not supported for inbound workers")

    url = f"{_worker_http_base(worker)}/internal/download"
    return await _stream_from_worker(
        "GET", url, worker=worker, params={"project_path": project_path, "path": path}
    )


@app.get("/api/zip")
async def zip_proxy(
    request: Request,
    worker_id: str,
    project_path: str,
    base: str = "",
    name: str = "files.zip",
) -> StreamingResponse:
    """Zip a set of project paths on a worker and stream the archive back.

    Paths arrive as repeated `path` query params so the whole thing is a plain
    anchor navigation (best mobile save UX). CF Access auth, then POST the list
    to the worker's /internal/zip.
    """
    await _auth_download_request(request)
    paths = request.query_params.getlist("path")
    if not paths:
        raise HTTPException(status_code=400, detail="at least one path required")

    worker = next((w for w in load_workers() if w.id == worker_id), None)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"unknown worker {worker_id}")
    if worker.mode == "inbound":
        raise HTTPException(status_code=501, detail="zip not supported for inbound workers")

    url = f"{_worker_http_base(worker)}/internal/zip"
    return await _stream_from_worker(
        "POST",
        url,
        worker=worker,
        json_body={
            "project_path": project_path,
            "base": base,
            "paths": paths,
            "filename": name,
        },
    )


@app.websocket("/worker-register")
async def worker_register_endpoint(websocket: WebSocket) -> None:
    """Reverse-mode control channel.

    A worker in `mode: "inbound"` dials this once at startup and keeps the
    connection open for its lifetime. The hub uses it to signal
    "open a data channel for browser session X" (see InboundRegistry).

    Auth: shared secret in Authorization header (same WORKER_SECRET as the
    outbound path uses, just on the other end of the wire). When the hub
    itself sits behind Cloudflare Access, that gate is applied at the CF
    edge using a service token; this endpoint just sees the resulting
    authenticated request like any other.

    Handshake: first frame must be {type: "register", payload: {id, type?,
    label?, models?}}. id must match a workers.json entry with
    mode=="inbound" (other entries are rejected to avoid stray
    registrations from misconfigured workers stomping on outbound slots).
    """
    expected = os.environ.get("WORKER_SECRET", "")
    auth = websocket.headers.get("authorization", "")
    if expected and (not auth.startswith("Bearer ") or auth[7:] != expected):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("worker_register_auth_rejected", client=websocket.client)
        return

    await websocket.accept()

    # First frame: register handshake. Bound time so a misbehaving worker
    # that never sends doesn't leak the slot forever.
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        handshake = json.loads(raw)
    except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
        logger.warning("worker_register_bad_handshake")
        return

    if handshake.get("type") != "register":
        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
        logger.warning("worker_register_bad_handshake_type", got=handshake.get("type"))
        return

    payload = handshake.get("payload") or {}
    worker_id = payload.get("id")
    if not isinstance(worker_id, str) or not worker_id:
        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
        logger.warning("worker_register_missing_id")
        return

    # Check this worker_id is one we expect in inbound mode. Reject silent
    # registrations from workers not in the config so a stolen secret can't
    # be used to inject an unconfigured worker.
    cfg = next((w for w in load_workers() if w.id == worker_id), None)
    if cfg is None or cfg.mode != "inbound":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning(
            "worker_register_unknown_or_wrong_mode",
            worker_id=worker_id,
            configured=cfg is not None,
            configured_mode=cfg.mode if cfg else None,
        )
        return

    entry = inbound_registry.register_control(
        worker_id=worker_id,
        ws=websocket,
        worker_type=payload.get("type") or cfg.type,
        label=payload.get("label") or cfg.label,
        models=payload.get("models") or [],
    )

    # Send ack so the worker knows registration succeeded and can start
    # reacting to commands.
    try:
        await websocket.send_text(json.dumps({"type": "registered", "payload": {}}))
    except Exception:
        inbound_registry.drop_control(worker_id, websocket)
        return

    # Hold the control channel open. We don't expect message traffic from
    # the worker on this channel (data goes over per-session data channels)
    # except for occasional acks/heartbeats. Drop on disconnect or any
    # I/O error - the worker will reconnect with backoff.
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # The only inbound message we currently care about is a heartbeat
            # ping; everything else is logged for debugging and ignored.
            if msg.get("type") == "ping":
                try:
                    await websocket.send_text(json.dumps({"type": "pong", "payload": {}}))
                except Exception:
                    break
            else:
                logger.debug("worker_control_unknown_msg", worker_id=worker_id, msg_type=msg.get("type"))
    finally:
        inbound_registry.drop_control(worker_id, websocket)


@app.websocket("/worker-data")
async def worker_data_endpoint(
    websocket: WebSocket,
    worker_id: str,
    session_id: str,
) -> None:
    """Reverse-mode per-session data channel.

    The worker dials this in response to an `open_data_channel` command
    sent over its control WS. The pairing is done by `session_id` (a UUID
    minted by InboundRegistry.open_session()).

    Auth: same WORKER_SECRET as /worker-register. Plus a check that the
    declared worker_id has a live control channel - otherwise a leaked
    secret alone wouldn't be enough to inject a data WS.
    """
    expected = os.environ.get("WORKER_SECRET", "")
    auth = websocket.headers.get("authorization", "")
    if expected and (not auth.startswith("Bearer ") or auth[7:] != expected):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("worker_data_auth_rejected", client=websocket.client)
        return

    if not inbound_registry.has_control(worker_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("worker_data_no_control", worker_id=worker_id, session_id=session_id)
        return

    await websocket.accept()

    # Attach a "done" event that the InboundTransport.close() will set
    # when WorkerConnection finishes with this WS. We then return from
    # this endpoint coroutine, which lets FastAPI/Starlette finalize the
    # WS lifecycle cleanly.
    done_event: asyncio.Event = asyncio.Event()
    websocket.state.inbound_done = done_event  # type: ignore[attr-defined]

    if not inbound_registry.register_data(session_id, websocket):
        # Stale session_id (waiter timed out, worker dialed too late, or
        # spurious connection). Close cleanly so the worker can log it.
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        logger.warning("worker_data_no_waiter", worker_id=worker_id, session_id=session_id)
        return

    # Block until the WorkerConnection that adopted this WS is done. The
    # InboundTransport.close() path sets done_event after closing the WS;
    # if the worker side closes first, the read loop catches the
    # disconnect and calls close() which also sets the event.
    try:
        await done_event.wait()
    except asyncio.CancelledError:
        pass


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
