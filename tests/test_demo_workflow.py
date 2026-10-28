from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from app.core import security
from demo_api.main import app as demo_app


def test_demo_v1_pass_and_v2_breaking_failures(client: TestClient, monkeypatch) -> None:
    from app.services import runner

    monkeypatch.setattr(security, "resolve_target_ips", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(runner, "assert_safe_url", lambda url: None)
    monkeypatch.setenv("DEMO_API_VERSION", "v1")

    demo = TestClient(demo_app)
    from pathlib import Path

    spec_text = Path("demo_api/openapi.yaml").read_text(encoding="utf-8")
    project = client.post("/projects", json={"name": "workflow"}).json()
    uploaded = client.post(
        f"/projects/{project['id']}/specs",
        files={"file": ("openapi.yaml", spec_text, "application/yaml")},
    )
    assert uploaded.status_code == 201, uploaded.text
    environment = client.post(
        f"/projects/{project['id']}/environments",
        json={"name": "demo", "base_url": "https://api.example.test", "auth_type": "bearer"},
    )
    assert environment.status_code == 201, environment.text

    def respond(request: httpx.Request) -> httpx.Response:
        response = demo.get(
            f"http://testserver{request.url.path}",
            params=dict(request.url.params),
            headers=dict(request.headers),
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    run_payload = {
        "spec_id": uploaded.json()["id"],
        "environment_id": environment.json()["id"],
        "auth": {"token": "demo-token"},
    }
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.example.test/health").mock(side_effect=respond)
        router.get("https://api.example.test/users/u-1").mock(side_effect=respond)
        router.get("https://api.example.test/users", params={"limit": "10"}).mock(
            side_effect=respond
        )
        router.get("https://api.example.test/account").mock(side_effect=respond)

        v1 = client.post("/runs", json=run_payload)
        assert v1.status_code == 201, v1.text
        assert v1.json()["summary"]["failed"] == 0
        assert v1.json()["summary"]["passed"] == 4

        monkeypatch.setenv("DEMO_API_VERSION", "v2-breaking")
        breaking = client.post("/runs", json=run_payload)
        assert breaking.status_code == 201, breaking.text

    changed = breaking.json()
    assert changed["summary"]["failed"] >= 2
    user_result = next(item for item in changed["results"] if item["operation_id"] == "getUser")
    paths = {item["path"] for item in user_result["violations"]}
    assert "body.name" in paths
    assert "body.active" in paths
    account = next(item for item in changed["results"] if item["operation_id"] == "getAccount")
    assert account["auth_hint"] and "API key" in account["auth_hint"]
