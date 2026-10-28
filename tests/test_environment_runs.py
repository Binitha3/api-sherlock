from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from app.core import security


def test_environment_crud(client: TestClient, project_id: str) -> None:
    created = client.post(
        f"/projects/{project_id}/environments",
        json={
            "name": " Local ",
            "base_url": "http://api.example.test/v1/",
            "auth_type": "api_key_header",
            "auth_header_name": "X-Token",
        },
    )
    assert created.status_code == 201, created.text
    environment = created.json()
    assert environment["name"] == "Local"
    assert environment["base_url"] == "http://api.example.test/v1"
    assert environment["auth_type"] == "api_key_header"
    assert "token" not in environment

    listed = client.get(f"/projects/{project_id}/environments")
    assert [item["id"] for item in listed.json()["environments"]] == [environment["id"]]
    assert client.get(f"/projects/{project_id}/environments/{environment['id']}").status_code == 200

    changed = client.patch(
        f"/projects/{project_id}/environments/{environment['id']}",
        json={"name": "Staging", "auth_type": "bearer"},
    )
    assert changed.status_code == 200
    assert changed.json()["name"] == "Staging"
    assert changed.json()["auth_type"] == "bearer"

    assert (
        client.delete(f"/projects/{project_id}/environments/{environment['id']}").status_code == 204
    )
    assert client.get(f"/projects/{project_id}/environments/{environment['id']}").status_code == 404


def test_environment_rejects_credentials_in_base_url(client: TestClient, project_id: str) -> None:
    response = client.post(
        f"/projects/{project_id}/environments",
        json={"name": "unsafe", "base_url": "https://user:password@api.example.test"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_invalid"


@respx.mock
def test_run_persists_results_but_not_auth_secrets(
    client: TestClient, project_id: str, uploaded_spec_id: str, monkeypatch
) -> None:
    monkeypatch.setattr(security, "resolve_target_ips", lambda host, port: ["93.184.216.34"])
    env_response = client.post(
        f"/projects/{project_id}/environments",
        json={
            "name": "Test",
            "base_url": "https://api.example.test",
            "auth_type": "bearer",
        },
    )
    assert env_response.status_code == 201, env_response.text
    environment_id = env_response.json()["id"]
    route = respx.get("https://api.example.test/users", params={"limit": "10"})
    route.mock(return_value=httpx.Response(200, json=[{"id": "u-1", "email": "a@example.test"}]))

    response = client.post(
        "/runs",
        json={
            "spec_id": uploaded_spec_id,
            "environment_id": environment_id,
            "include_operations": ["listUsers"],
            "auth": {"token": "never-persist-this-token"},
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "completed"
    assert data["summary"] == {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
    assert data["results"][0]["operation_id"] == "listUsers"
    assert data["results"][0]["status"] == "passed"
    assert route.calls[0].request.headers["authorization"] == "Bearer never-persist-this-token"

    fetched = client.get(f"/runs/{data['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["results"] == data["results"]
    assert "never-persist-this-token" not in fetched.text
