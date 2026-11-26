"""Path-based reverse proxy: /{port}/... -> http://PROXY_HOST:{port}/...

Lets you reach any service listening on the host by URL path, e.g.
    https://hub.example.com/8005/     ->  http://172.23.60.65:8005/
    https://hub.example.com/8005/foo  ->  http://172.23.60.65:8005/foo

Both plain HTTP and WebSocket (dev-server HMR, etc.) are proxied. Requests
already pass Cloudflare Access at the edge, so no extra auth is added here.

The routes MUST be registered before the catch-all static mount in main.py.
The ``:int`` path converter means these only ever match a numeric first
segment, so /api/*, /ws and /assets/* are never shadowed.
"""

import asyncio
import os

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

logger = structlog.get_logger()

# Host that actually runs the target services. Hardcoded to the WSL2 host by
# default; override with PROXY_HOST if the deployment moves.
PROXY_HOST = os.environ.get("PROXY_HOST", "172.23.60.65")

_MIN_PORT = 1
_MAX_PORT = 65535
# Never proxy to the hub's own port - avoids an accidental request loop.
_BLOCKED_PORTS = {8001}

# Hop-by-hop headers (RFC 7230 6.1) must not be forwarded by a proxy.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # read=None: don't time out long-lived streaming responses / SSE.
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10.0),
            follow_redirects=False,
        )
    return _client


def _allowed(port: int) -> bool:
    return _MIN_PORT <= port <= _MAX_PORT and port not in _BLOCKED_PORTS


def _rewrite_location(loc: str, port: int) -> str:
    """Keep the /{port} prefix on server-issued redirects so the client stays
    inside the proxied namespace."""
    prefix = f"http://{PROXY_HOST}:{port}"
    if loc.startswith(prefix):
        rest = loc[len(prefix) :]
        return f"/{port}{rest or '/'}"
    # Path-absolute redirect ("/login") - but not protocol-relative ("//host").
    if loc.startswith("/") and not loc.startswith("//"):
        return f"/{port}{loc}"
    return loc


async def _proxy_http(port: int, path: str, request: Request) -> Response:
    if not _allowed(port):
        raise HTTPException(status_code=403, detail=f"port {port} is not proxyable")

    target = f"http://{PROXY_HOST}:{port}/{path}"
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in ("host", "content-length")
    }

    client = _get_client()
    upstream_req = client.build_request(
        request.method,
        target,
        headers=fwd_headers,
        params=request.query_params,
        content=request.stream(),
    )
    try:
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise HTTPException(status_code=502, detail=f"cannot reach {PROXY_HOST}:{port}") from None
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e

    resp_headers = [
        (k, _rewrite_location(v, port) if k.lower() == "location" else v)
        for k, v in upstream.headers.multi_items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"
    ]

    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    # Set raw headers directly to preserve duplicates (e.g. multiple Set-Cookie).
    response.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in resp_headers
    ]
    return response


async def _pump(client_ws: WebSocket, upstream: ClientConnection) -> None:
    """Shuttle frames both ways until either side closes."""

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if (text := msg.get("text")) is not None:
                    await upstream.send(text)
                elif (data := msg.get("bytes")) is not None:
                    await upstream.send(data)
        except (WebSocketDisconnect, ConnectionClosed, RuntimeError):
            pass
        finally:
            await upstream.close()

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await client_ws.send_bytes(message)
                else:
                    await client_ws.send_text(message)
        except ConnectionClosed:
            pass
        finally:
            try:
                await client_ws.close()
            except RuntimeError:
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client())


async def _proxy_ws(port: int, path: str, ws: WebSocket) -> None:
    if not _allowed(port):
        await ws.close(code=1008)
        return

    query = ws.url.query
    target = f"ws://{PROXY_HOST}:{port}/{path}" + (f"?{query}" if query else "")

    requested = ws.headers.get("sec-websocket-protocol")
    subprotocols = [p.strip() for p in requested.split(",")] if requested else None

    try:
        upstream = await ws_connect(target, subprotocols=subprotocols, max_size=None)
    except (OSError, ConnectionClosed, InvalidHandshake) as e:
        logger.info("port_proxy_ws_connect_failed", port=port, error=str(e))
        await ws.close(code=1011)
        return

    await ws.accept(subprotocol=upstream.subprotocol)
    try:
        await _pump(ws, upstream)
    finally:
        await upstream.close()


def register_port_proxy(app: FastAPI) -> None:
    """Wire the /{port}/... proxy routes onto the hub app.

    Call this BEFORE mounting the static frontend at '/'."""

    @app.websocket("/{port:int}/{path:path}")
    async def _ws_route(ws: WebSocket, port: int, path: str) -> None:
        await _proxy_ws(port, path, ws)

    @app.websocket("/{port:int}")
    async def _ws_route_root(ws: WebSocket, port: int) -> None:
        await _proxy_ws(port, "", ws)

    @app.get("/{port:int}")
    async def _redirect_to_slash(port: int, request: Request) -> RedirectResponse:
        # A bare /{port} has no trailing slash, so the target app's relative
        # asset paths would resolve against '/' and 404. Redirect to /{port}/.
        if not _allowed(port):
            raise HTTPException(status_code=403, detail=f"port {port} is not proxyable")
        suffix = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/{port}/{suffix}")

    @app.api_route("/{port:int}/{path:path}", methods=_HTTP_METHODS)
    async def _http_route(port: int, path: str, request: Request) -> Response:
        return await _proxy_http(port, path, request)
