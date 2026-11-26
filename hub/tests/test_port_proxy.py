"""Tests for hub.src.proxy.port_proxy - auth gate in front of the port proxy.

No upstream is needed: port 8001 (the hub's own port) is always refused
with 403, so a 403 proves a request got PAST the auth check, while a 401 /
WS close 1008 proves it was stopped by it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.auth import cf_access
from src.proxy import port_proxy

BLOCKED = 8001


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    port_proxy.register_port_proxy(app)
    return TestClient(app)


# ── Real verify_cf_access_token: prod without CF config is fail-closed ──


@patch.object(cf_access, "CLAUDE_WEB_ENV", "prod")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
def test_prod_without_cf_config_rejects_http(client: TestClient) -> None:
    resp = client.get(f"/{BLOCKED}/anything")
    assert resp.status_code == 401


@patch.object(cf_access, "CLAUDE_WEB_ENV", "prod")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
def test_prod_without_cf_config_rejects_bare_port_redirect(client: TestClient) -> None:
    resp = client.get(f"/{BLOCKED}", follow_redirects=False)
    assert resp.status_code == 401


@patch.object(cf_access, "CLAUDE_WEB_ENV", "prod")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
def test_prod_without_cf_config_rejects_websocket(client: TestClient) -> None:
    # The blocked port also closes with 1008, so the code alone proves
    # nothing - assert the port policy was never consulted.
    seen: list[int] = []
    real_allowed = port_proxy._allowed

    def spy(port: int) -> bool:
        seen.append(port)
        return real_allowed(port)

    with patch.object(port_proxy, "_allowed", spy):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/{BLOCKED}/hmr"):
                pass
    assert exc.value.code == 1008
    assert seen == []


@patch.object(cf_access, "CLAUDE_WEB_ENV", "dev")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
def test_dev_bypass_reaches_the_port_check(client: TestClient) -> None:
    # Same dev behaviour as the rest of the hub: auth passes, and the
    # request is then judged on its port.
    resp = client.get(f"/{BLOCKED}/anything")
    assert resp.status_code == 403


# ── Token sources: header and cookie, as the upload route accepts ──────


async def _only_good_token(token: str | None) -> dict[str, Any] | None:
    return {"email": "me@example.com"} if token == "good-token" else None


@patch.object(port_proxy, "verify_cf_access_token", _only_good_token)
def test_missing_or_bad_token_is_rejected(client: TestClient) -> None:
    assert client.get(f"/{BLOCKED}/x").status_code == 401
    bad = client.get(f"/{BLOCKED}/x", headers={"cf-access-jwt-assertion": "nope"})
    assert bad.status_code == 401


@patch.object(port_proxy, "verify_cf_access_token", _only_good_token)
def test_header_token_is_accepted(client: TestClient) -> None:
    resp = client.get(f"/{BLOCKED}/x", headers={"cf-access-jwt-assertion": "good-token"})
    assert resp.status_code == 403  # past auth, stopped by the port policy


@patch.object(port_proxy, "verify_cf_access_token", _only_good_token)
def test_cookie_token_is_accepted(client: TestClient) -> None:
    client.cookies.set("CF_Authorization", "good-token")
    resp = client.get(f"/{BLOCKED}/x")
    assert resp.status_code == 403


@patch.object(port_proxy, "verify_cf_access_token", _only_good_token)
def test_auth_runs_before_the_port_check(client: TestClient) -> None:
    # An unauthenticated caller must not be able to probe port policy:
    # a blocked port answers 401, not 403.
    assert client.get(f"/{BLOCKED}/x").status_code == 401


@patch.object(port_proxy, "verify_cf_access_token", _only_good_token)
def test_websocket_auth_runs_before_the_port_check(client: TestClient) -> None:
    # Auth and the port policy both close a WS with 1008, so tell them apart
    # by whether the port check ran at all.
    seen: list[int] = []
    real_allowed = port_proxy._allowed

    def spy(port: int) -> bool:
        seen.append(port)
        return real_allowed(port)

    with patch.object(port_proxy, "_allowed", spy):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/{BLOCKED}/hmr", headers={"cf-access-jwt-assertion": "good-token"}
            ):
                pass
        assert seen == [BLOCKED]  # authenticated: reached the port policy

        seen.clear()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/{BLOCKED}/hmr"):
                pass
        assert seen == []  # rejected before the port was even looked at


@patch.object(port_proxy, "verify_cf_access_token", _only_good_token)
def test_websocket_without_token_is_closed_1008(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/{BLOCKED}/hmr"):
            pass
    assert exc.value.code == 1008
