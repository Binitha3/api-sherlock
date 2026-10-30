from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app
from demo_api.main import app as demo_app

SPEC = Path("demo_api/openapi.yaml")
BASE_URL = "https://api.example.test"
PATHS = ("/health", "/users/u-1", "/users", "/account")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import security

    monkeypatch.setattr(security, "resolve_target_ips", lambda host, port: ["93.184.216.34"])
    monkeypatch.setenv("SHERLOCK_TOKEN", "demo-token")
    monkeypatch.setenv("DEMO_API_VERSION", "v1")


def _respond(request: httpx.Request) -> httpx.Response:
    demo = TestClient(demo_app)
    response = demo.get(
        f"http://testserver{request.url.path}",
        params=dict(request.url.params),
        headers=dict(request.headers),
    )
    return httpx.Response(
        response.status_code, headers=dict(response.headers), content=response.content
    )


def _router() -> respx.Router:
    router = respx.mock(assert_all_called=False)
    router.get(f"{BASE_URL}/health").mock(side_effect=_respond)
    router.get(f"{BASE_URL}/users/u-1").mock(side_effect=_respond)
    router.get(f"{BASE_URL}/users", params={"limit": "10"}).mock(side_effect=_respond)
    router.get(f"{BASE_URL}/account").mock(side_effect=_respond)
    return router


def _check(*args: str) -> object:
    return CliRunner().invoke(app, ["check", *args])


def _save_baseline(path: Path) -> object:
    return CliRunner().invoke(
        app,
        [
            "baseline",
            "save",
            "--spec",
            str(SPEC),
            "--base-url",
            BASE_URL,
            "--auth-type",
            "bearer",
            "--output",
            str(path),
        ],
    )


def test_baseline_save_writes_shapes_not_values(tmp_path: Path) -> None:
    baseline_file = tmp_path / "baseline.json"
    with _router():
        result = _save_baseline(baseline_file)
    assert result.exit_code == 0, result.stderr

    payload = json.loads(baseline_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["operations"]) == {
        "GET /health",
        "GET /users/{user_id}",
        "GET /users",
        "GET /account",
    }
    user_schema = payload["operations"]["GET /users/{user_id}"]["200"]["schema"]
    assert sorted(user_schema["required"]) == ["active", "id", "name"]
    text = baseline_file.read_text(encoding="utf-8")
    assert "Ada Lovelace" not in text  # values are never stored
    assert "demo-token" not in text


def test_check_clean_run_exits_zero(tmp_path: Path) -> None:
    baseline_file = tmp_path / "baseline.json"
    with _router():
        assert _save_baseline(baseline_file).exit_code == 0

    with _router():
        result = _check(
            "--spec",
            str(SPEC),
            "--base-url",
            BASE_URL,
            "--auth-type",
            "bearer",
            "--baseline",
            str(baseline_file),
        )
    assert result.exit_code == 0, result.stderr
    assert "OK: no changes at or above 'breaking' severity." in result.stderr


def test_check_fails_on_breaking_demo_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_file = tmp_path / "baseline.json"
    report_file = tmp_path / "report.md"
    with _router():
        assert _save_baseline(baseline_file).exit_code == 0

    monkeypatch.setenv("DEMO_API_VERSION", "v2-breaking")
    with _router():
        result = _check(
            "--spec",
            str(SPEC),
            "--base-url",
            BASE_URL,
            "--auth-type",
            "bearer",
            "--baseline",
            str(baseline_file),
            "--fail-on",
            "breaking",
            "--format",
            "markdown",
            "--output",
            str(report_file),
        )

    assert result.exit_code == 1, result.stderr
    report = report_file.read_text(encoding="utf-8")
    assert "# API Sherlock report" in report
    assert "GET /users/{user_id} -> 200 -> body.name" in report
    assert "GET /users/{user_id} -> 200 -> body.active" in report
    assert "Breaking" in report
    assert "HTTP 200" in report


def test_check_threshold_controls_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_file = tmp_path / "baseline.json"
    with _router():
        assert _save_baseline(baseline_file).exit_code == 0

    monkeypatch.setenv("DEMO_API_VERSION", "v2-safe")
    with _router():
        lenient = _check(
            "--spec",
            str(SPEC),
            "--base-url",
            BASE_URL,
            "--auth-type",
            "bearer",
            "--baseline",
            str(baseline_file),
            "--fail-on",
            "warning",
            "--format",
            "json",
        )
    assert lenient.exit_code == 0, lenient.stderr
    assert json.loads(lenient.stdout)["summary"]["changes"]["non_breaking"] >= 1

    with _router():
        strict = _check(
            "--spec",
            str(SPEC),
            "--base-url",
            BASE_URL,
            "--auth-type",
            "bearer",
            "--baseline",
            str(baseline_file),
            "--fail-on",
            "non_breaking",
            "--format",
            "json",
        )
    assert strict.exit_code == 1, strict.stderr


def test_check_reports_auth_hints() -> None:
    with _router():
        result = _check("--spec", str(SPEC), "--base-url", BASE_URL, "--format", "json")
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    account = next(item for item in report["operations"] if item["operation_id"] == "getAccount")
    assert account["http_status"] == 401
    assert "authentication" in account["auth_hint"]


def test_check_tool_errors_exit_two(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("openapi: 3.0.3\npaths: {}\n", encoding="utf-8")
    bad_spec = _check("--spec", str(invalid), "--base-url", BASE_URL)
    assert bad_spec.exit_code == 2
    assert "error:" in bad_spec.stderr

    bad_format = _check("--spec", str(SPEC), "--base-url", BASE_URL, "--format", "pdf")
    assert bad_format.exit_code == 2
    assert "unknown format" in bad_format.stderr

    bad_fail_on = _check("--spec", str(SPEC), "--base-url", BASE_URL, "--fail-on", "sometimes")
    assert bad_fail_on.exit_code == 2

    missing_baseline = _check(
        "--spec", str(SPEC), "--base-url", BASE_URL, "--baseline", str(tmp_path / "nope.json")
    )
    assert missing_baseline.exit_code == 2
    assert "baseline file" in missing_baseline.stderr


def test_check_requires_auth_secret_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHERLOCK_TOKEN")
    result = _check("--spec", str(SPEC), "--base-url", BASE_URL, "--auth-type", "bearer")
    assert result.exit_code == 2
    assert "SHERLOCK_TOKEN" in result.stderr
