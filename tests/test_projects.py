"""Project CRUD tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_project(client: TestClient) -> None:
    response = client.post("/projects", json={"name": "Payments", "description": "Stripe-like"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Payments"
    assert body["description"] == "Stripe-like"
    assert len(body["id"]) == 32
    assert body["created_at"]
    assert body["spec_count"] == 0


def test_create_project_requires_name(client: TestClient) -> None:
    response = client.post("/projects", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_invalid"


def test_create_project_rejects_blank_name(client: TestClient) -> None:
    response = client.post("/projects", json={"name": "   "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_invalid"


def test_list_projects(client: TestClient) -> None:
    assert client.get("/projects").json() == {"projects": []}
    client.post("/projects", json={"name": "One"})
    client.post("/projects", json={"name": "Two"})
    body = client.get("/projects").json()
    assert [p["name"] for p in body["projects"]] == ["Two", "One"]


def test_specs_for_unknown_project_is_404(client: TestClient) -> None:
    response = client.get("/projects/00000000000000000000000000000000/specs")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "00000000000000000000000000000000" in body["error"]["message"]


def test_import_for_unknown_project_is_404(client: TestClient) -> None:
    response = client.post(
        "/projects/00000000000000000000000000000000/specs",
        files={"file": ("openapi.yaml", "openapi: 3.0.3", "application/yaml")},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
