"""API-key protection and CORS configuration (Phase 5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings


def test_health_stays_public(client: TestClient) -> None:
    assert client.get("/health", headers={"X-API-Key": ""}).status_code == 200


def test_routes_require_the_configured_api_key(client: TestClient) -> None:
    missing = client.get("/projects", headers={"X-API-Key": ""})
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert "X-API-Key" in missing.json()["error"]["message"]

    wrong = client.get("/projects", headers={"X-API-Key": "not-the-key"})
    assert wrong.status_code == 401

    created = client.post("/projects", json={"name": "protected"})
    assert created.status_code == 201
    assert len(client.get("/projects").json()["projects"]) == 1


def test_openapi_docs_are_also_protected(client: TestClient) -> None:
    assert client.get("/docs", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/docs").status_code == 200


def test_cors_allows_the_configured_origin(client: TestClient) -> None:
    origin = get_settings().cors_origin_list[0]
    response = client.get("/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin

    preflight = client.options(
        "/projects",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key,content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert "x-api-key" in preflight.headers["access-control-allow-headers"].lower()


def test_cors_rejects_unknown_origins(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
