"""Tests for WebSocket endpoint — Phase 1 ping/pong."""

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "connections" in data


def test_ws_ping_pong(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping", "payload": {}})
        resp = ws.receive_json()
        assert resp["type"] == "pong"


def test_ws_unknown_type(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "unknown_thing", "payload": {}})
        resp = ws.receive_json()
        assert resp["type"] == "error"
        assert resp["payload"]["code"] == "not_implemented"


def test_ws_invalid_json(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not json at all")
        resp = ws.receive_json()
        assert resp["type"] == "error"
        assert resp["payload"]["code"] == "invalid_json"
