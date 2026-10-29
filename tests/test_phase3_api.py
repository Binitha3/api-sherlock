from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def _upload_spec(client: TestClient, project_id: str, document: str) -> str:
    response = client.post(
        f"/projects/{project_id}/specs",
        files={"file": ("spec.yaml", document, "application/yaml")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_spec_diff_reports_nested_schema_and_auth_changes(
    client: TestClient, project_id: str
) -> None:
    old_spec = """
openapi: 3.0.3
info: {title: API, version: v1}
paths:
  /user:
    get:
      operationId: user
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/User'}
components:
  schemas:
    User:
      type: object
      required: [id, address]
      properties:
        id: {type: string}
        address:
          type: object
          required: [zip]
          properties:
            zip: {type: string}
  securitySchemes:
    bearer: {type: http, scheme: bearer}
security:
  - bearer: []
"""
    new_spec = """
openapi: 3.0.3
info: {title: API, version: v2}
paths:
  /user:
    get:
      operationId: user
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/User'}
components:
  schemas:
    User:
      type: object
      required: [id, address]
      properties:
        id: {type: integer}
        address:
          type: object
          properties:
            zip: {type: integer}
  securitySchemes:
    key: {type: apiKey, in: header, name: X-API-Key}
security:
  - key: []
"""
    old_id = _upload_spec(client, project_id, old_spec)
    new_id = _upload_spec(client, project_id, new_spec)
    response = client.post(
        f"/projects/{project_id}/diff/specs",
        json={"old_spec_id": old_id, "new_spec_id": new_id},
    )
    assert response.status_code == 200, response.text
    changes = response.json()["changes"]
    assert response.json()["summary"]["breaking"] >= 3
    assert any(change["path"] == "GET /user -> 200 -> body.id" for change in changes)
    assert any(change["path"] == "GET /user -> 200 -> body.address.zip" for change in changes)
    assert any(change["kind"] == "required_removed" for change in changes)
    assert any(change["kind"] == "security_changed" for change in changes)


def test_baseline_save_then_live_run_diff(client: TestClient, project_id: str, monkeypatch) -> None:
    import app.api.routes as api_routes

    spec_id = _upload_spec(
        client,
        project_id,
        """
openapi: 3.0.3
info: {title: API, version: v1}
paths:
  /user:
    get:
      operationId: user
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                required: [id]
                properties:
                  id: {type: string}
""",
    )
    environment = client.post(
        f"/projects/{project_id}/environments",
        json={"name": "test", "base_url": "https://api.example.test"},
    )
    assert environment.status_code == 201, environment.text

    def observed(name: bool) -> list[dict[str, object]]:
        properties: dict[str, object] = {"id": {"type": "string"}}
        if name:
            properties["name"] = {"type": "string"}
        return [
            {
                "operation_id": "user",
                "method": "GET",
                "path": "/user",
                "status": "passed",
                "http_status": 200,
                "latency_ms": 3.5,
                "content_type": "application/json",
                "violations": [],
                "auth_hint": None,
                "error_code": None,
                "error_message": None,
                "observed_schema": {
                    "200": {
                        "content_type": "application/json",
                        "schema": {
                            "type": "object",
                            "required": sorted(properties),
                            "properties": properties,
                        },
                    }
                },
            }
        ]

    monkeypatch.setattr(
        api_routes, "run_operations", AsyncMock(side_effect=[observed(False), observed(True)])
    )
    run_payload = {"spec_id": spec_id, "environment_id": environment.json()["id"]}
    first = client.post("/runs", json=run_payload)
    assert first.status_code == 201, first.text
    baseline = client.post(f"/runs/{first.json()['id']}/baseline", json={"label": "v1"})
    assert baseline.status_code == 201, baseline.text

    second = client.post("/runs", json=run_payload)
    assert second.status_code == 201, second.text
    response = client.get(
        f"/runs/{second.json()['id']}/diff", params={"baseline_id": baseline.json()["id"]}
    )
    assert response.status_code == 200, response.text
    changes = response.json()["changes"]
    assert len(changes) == 1
    assert changes[0]["path"] == "GET /user -> 200 -> body.name"
    assert changes[0]["severity"] == "non_breaking"
    assert (
        client.get(f"/projects/{project_id}/baselines").json()["baselines"][0]["id"]
        == baseline.json()["id"]
    )
