"""Normalizing proxy between Claude Code and a llama.cpp server.

llama.cpp serves the Anthropic wire format natively on /v1/messages, so the
stock Claude Code CLI can talk to it unmodified - with one exception.

Claude Code appends its MCP-server and skills instructions as a trailing
message with role "system" INSIDE `messages`:

    messages: [ {role: user, ...}, {role: system, content: "# MCP Server..."} ]

That is not something the public Anthropic API accepts, but the CLI emits it
and api.anthropic.com tolerates it. llama.cpp passes it straight into the
model's Jinja chat template, and Qwen3's template hard-fails on it:

    Jinja Exception: System message must be at the beginning.  -> HTTP 500

So every single turn dies before the model sees a token. This shim hoists any
such message out of `messages` and appends it to the top-level `system` array,
which is semantically what the CLI meant and what every template accepts.

Everything else is passed through untouched, including SSE streaming - the
response body is relayed chunk by chunk so token-by-token streaming in the web
UI still works.

Run: uvicorn llama_shim:app --host 0.0.0.0 --port 8020
Env: LLAMA_UPSTREAM (default http://192.168.1.25:8080), SHIM_PORT.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

logger = structlog.get_logger()

UPSTREAM = os.environ.get("LLAMA_UPSTREAM", "http://192.168.1.25:8080").rstrip("/")
PORT = int(os.environ.get("SHIM_PORT", "8020"))

# Headers a proxy must not forward verbatim (RFC 7230 6.1 + length/host).
_DROP = frozenset({
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "te", "trailers", "proxy-authenticate", "proxy-authorization",
})

app = FastAPI(title="llama-shim")

# read=None: never time out a long generation. A 27B model on CPU/partial
# offload can sit silent for minutes before the first token.
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=None)
_client: httpx.AsyncClient | None = None


def _blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize an Anthropic content field to a list of text blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def normalize(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Hoist role:"system" messages into the top-level `system` array.

    Returns (body, hoisted_count). The body is mutated in place; callers that
    care about the original should copy first. Order is preserved: hoisted
    blocks land after the existing system content, still ahead of the
    conversation, which is where the CLI intended them to be read.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, 0

    hoisted: list[dict[str, Any]] = []
    kept: list[Any] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            hoisted.extend(_blocks(m.get("content")))
        else:
            kept.append(m)

    if not hoisted:
        return body, 0

    body["system"] = _blocks(body.get("system")) + hoisted
    body["messages"] = kept
    return body, len(hoisted)


# Registered BEFORE the catch-all so it is answered locally instead of being
# forwarded upstream (llama.cpp has no /healthz). Deliberately does not probe
# the upstream: the shim being up is what the healthcheck is about, and a
# restarting llama.cpp should not flap this container.
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "upstream": UPSTREAM}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def proxy(path: str, request: Request) -> Response:
    assert _client is not None
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP}

    # Only /v1/messages carries the malformed shape; leave anything else
    # (e.g. /v1/models) byte-identical so we never corrupt an unknown route.
    if raw and path.startswith("v1/messages"):
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("shim_unparseable_body", size=len(raw))
        else:
            if isinstance(body, dict):
                body, hoisted = normalize(body)
                if hoisted:
                    raw = json.dumps(body).encode()
                # Request size is the thing that matters most here: the model
                # has a 98k window and the tool schemas alone can eat a
                # quarter of it. Logging it makes context bloat visible
                # instead of something you discover as a truncated answer.
                logger.info(
                    "shim_request",
                    bytes=len(raw),
                    tools=len(body.get("tools") or []),
                    messages=len(body.get("messages") or []),
                    hoisted=hoisted,
                )

    url = f"{UPSTREAM}/{path}"
    req = _client.build_request(
        request.method, url, content=raw, headers=headers,
        params=request.query_params,
    )
    upstream = await _client.send(req, stream=True)

    # Relay the body as it arrives so SSE streaming survives the hop.
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _DROP
    }
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream.aclose),
    )


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=_TIMEOUT)
    logger.info("shim_started", upstream=UPSTREAM, port=PORT)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
