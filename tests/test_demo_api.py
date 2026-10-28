from __future__ import annotations

from fastapi.testclient import TestClient

from demo_api.main import app


def test_demo_api_switches_versions(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setenv("DEMO_API_VERSION", "v1")
    v1 = client.get("/users/u-1").json()
    assert v1 == {"id": "u-1", "name": "Ada Lovelace", "active": True}

    monkeypatch.setenv("DEMO_API_VERSION", "v2-safe")
    safe = client.get("/users/u-1").json()
    assert safe["name"] == "Ada Lovelace"
    assert safe["plan"] == "free"

    monkeypatch.setenv("DEMO_API_VERSION", "v2-breaking")
    breaking = client.get("/users/u-1").json()
    assert "name" not in breaking
    assert breaking["active"] == "yes"


def test_demo_api_breaking_auth_uses_configured_header(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setenv("DEMO_API_VERSION", "v2-breaking")
    monkeypatch.setenv("DEMO_API_KEY_HEADER", "X-Custom-Demo-Key")
    monkeypatch.setenv("DEMO_API_KEY", "configured-demo-key")
    denied = client.get("/account", headers={"X-Demo-Key": "configured-demo-key"})
    assert denied.status_code == 401
    allowed = client.get("/account", headers={"X-Custom-Demo-Key": "configured-demo-key"})
    assert allowed.status_code == 200


def test_demo_api_bearer_route(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setenv("DEMO_API_VERSION", "v1")
    assert client.get("/account").status_code == 401
    authorized = client.get("/account", headers={"Authorization": "Bearer demo-token"})
    assert authorized.status_code == 200
