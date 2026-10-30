from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

USER_SPEC = """
openapi: 3.0.3
info: {title: Users, version: v1}
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
  /account:
    get:
      operationId: account
      security:
        - bearer: []
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  owner: {type: string}
components:
  securitySchemes:
    bearer: {type: http, scheme: bearer}
"""


def _result(
    operation_id: str,
    path: str,
    *,
    status: str = "passed",
    http_status: int = 200,
    latency_ms: float = 12.0,
    observed: dict | None = None,
    auth_hint: str | None = None,
    violations: list | None = None,
) -> dict:
    return {
        "operation_id": operation_id,
        "method": "GET",
        "path": path,
        "status": status,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "content_type": "application/json",
        "violations": violations or [],
        "auth_hint": auth_hint,
        "error_code": None,
        "error_message": None,
        "observed_schema": observed,
    }


def _shape(id_type: str) -> dict:
    return {
        "200": {
            "content_type": "application/json",
            "schema": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": id_type}},
            },
        }
    }


def _setup(client: TestClient, project_id: str) -> tuple[str, str]:
    spec = client.post(
        f"/projects/{project_id}/specs",
        files={"file": ("spec.yaml", USER_SPEC, "application/yaml")},
    )
    assert spec.status_code == 201, spec.text
    environment = client.post(
        f"/projects/{project_id}/environments",
        json={"name": "staging", "base_url": "https://api.example.test"},
    )
    assert environment.status_code == 201, environment.text
    return spec.json()["id"], environment.json()["id"]


def _completed_run(
    client: TestClient,
    monkeypatch,
    spec_id: str,
    environment_id: str,
    results: list,
) -> dict:
    monkeypatch.setattr("app.api.routes.run_operations", AsyncMock(return_value=results))
    response = client.post("/runs", json={"spec_id": spec_id, "environment_id": environment_id})
    assert response.status_code == 201, response.text
    return response.json()


def test_report_includes_baseline_changes_auth_hints_and_latency(
    client: TestClient, project_id: str, monkeypatch
) -> None:
    spec_id, environment_id = _setup(client, project_id)

    first = _completed_run(
        client,
        monkeypatch,
        spec_id,
        environment_id,
        [
            _result("user", "/user", observed=_shape("string"), latency_ms=10.0),
            _result("account", "/account", observed={"200": {"content_type": None, "schema": {}}}),
        ],
    )
    baseline = client.post(f"/runs/{first['id']}/baseline", json={"label": "v1"})
    assert baseline.status_code == 201, baseline.text

    second = _completed_run(
        client,
        monkeypatch,
        spec_id,
        environment_id,
        [
            _result(
                "user",
                "/user",
                status="failed",
                latency_ms=30.0,
                observed=_shape("integer"),
                violations=[
                    {
                        "path": "body.id",
                        "message": "Type mismatch.",
                        "expected": "string",
                        "actual": "integer",
                    }
                ],
            ),
            _result(
                "account",
                "/account",
                status="failed",
                http_status=401,
                latency_ms=20.0,
                auth_hint="The spec requires authentication but no credentials were sent.",
            ),
        ],
    )

    response = client.get(f"/runs/{second['id']}/report")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["baseline"]["id"] == baseline.json()["id"]
    assert report["summary"]["operations"] == {
        "total": 2,
        "passed": 0,
        "failed": 2,
        "skipped": 0,
    }
    assert report["summary"]["changes"]["breaking"] >= 1
    assert any(change["path"] == "GET /user -> 200 -> body.id" for change in report["changes"])
    assert report["failed_operations"][1]["auth_hint"].startswith(
        "The spec requires authentication"
    )
    assert report["latency"]["mean_ms"] == 25.0

    markdown = client.get(f"/runs/{second['id']}/report", params={"format": "markdown"})
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "# API Sherlock report" in markdown.text
    assert "GET /user -> 200 -> body.id" in markdown.text
    assert "Auth hint:" in markdown.text

    page = client.get(f"/runs/{second['id']}/report", params={"format": "html"})
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.text.startswith("<!DOCTYPE html>")
    assert "Breaking" in page.text


def test_report_does_not_compare_against_a_newer_baseline(
    client: TestClient, project_id: str, monkeypatch
) -> None:
    spec_id, environment_id = _setup(client, project_id)
    first = _completed_run(
        client,
        monkeypatch,
        spec_id,
        environment_id,
        [_result("user", "/user", observed=_shape("string"))],
    )
    saved = client.post(f"/runs/{first['id']}/baseline", json={"label": "later"})
    assert saved.status_code == 201, saved.text

    report = client.get(f"/runs/{first['id']}/report")
    assert report.status_code == 200, report.text
    assert report.json()["baseline"] is None
    assert report.json()["summary"]["changes"]["total"] == 0


def test_report_accepts_an_explicit_baseline(
    client: TestClient, project_id: str, monkeypatch
) -> None:
    spec_id, environment_id = _setup(client, project_id)
    first = _completed_run(
        client,
        monkeypatch,
        spec_id,
        environment_id,
        [_result("user", "/user", observed=_shape("string"))],
    )
    saved = client.post(f"/runs/{first['id']}/baseline", json={"label": "v1"})
    baseline_id = saved.json()["id"]

    report = client.get(f"/runs/{first['id']}/report", params={"baseline_id": baseline_id})
    assert report.status_code == 200, report.text
    assert report.json()["baseline"]["label"] == "v1"
    assert report.json()["summary"]["changes"]["total"] == 0


def test_report_rejects_unknown_baseline_and_format(
    client: TestClient, project_id: str, monkeypatch
) -> None:
    spec_id, environment_id = _setup(client, project_id)
    run = _completed_run(
        client,
        monkeypatch,
        spec_id,
        environment_id,
        [_result("user", "/user", observed=_shape("string"))],
    )
    missing = client.get(f"/runs/{run['id']}/report", params={"baseline_id": "nope"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    invalid = client.get(f"/runs/{run['id']}/report", params={"format": "pdf"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "request_invalid"

    assert client.get("/runs/unknown/report").status_code == 404
