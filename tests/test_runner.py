from __future__ import annotations

import httpx
import pytest
import respx

from app.services.runner import AuthContext, build_request, run_operations


@pytest.fixture(autouse=True)
def allow_loopback_for_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import security

    monkeypatch.setattr(security, "resolve_target_ips", lambda host, port: ["93.184.216.34"])


def test_build_request_uses_examples_and_auth() -> None:
    operation = {
        "operation_id": "getUser",
        "method": "GET",
        "path": "/users/{id}",
        "parameters": [
            {"name": "id", "in": "path", "required": True, "example": "u/1"},
            {"name": "limit", "in": "query", "schema": {"default": 5}},
        ],
    }
    prepared, skipped = build_request(
        operation,
        "https://api.example.test",
        None,
        AuthContext(auth_type="bearer", token="secret"),
    )
    assert skipped is None
    assert prepared is not None
    assert prepared.url == "https://api.example.test/users/u%2F1"
    assert prepared.params == {"limit": 5}
    assert prepared.headers == {"Authorization": "Bearer secret"}


def test_optional_request_body_does_not_skip_operation() -> None:
    prepared, skipped = build_request(
        {
            "method": "POST",
            "path": "/items",
            "parameters": [],
            "request_body": {"required": False, "content_types": ["application/json"]},
        },
        "https://api.example.test",
        None,
        AuthContext(),
    )
    assert prepared is not None
    assert skipped is None


def test_required_parameter_without_example_is_skipped() -> None:
    prepared, skipped = build_request(
        {
            "method": "GET",
            "path": "/users",
            "parameters": [{"name": "q", "in": "query", "required": True}],
        },
        "https://api.example.test",
        None,
        AuthContext(),
    )
    assert prepared is None
    assert skipped is not None
    assert skipped.code == "parameter_uninferable"


async def test_run_operations_validates_and_records_observed_schema() -> None:
    spec = {
        "paths": {},
        "components": {},
    }
    operation = {
        "operation_id": "getUser",
        "method": "GET",
        "path": "/users/u-1",
        "parameters": [],
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "string"}},
                        }
                    }
                }
            }
        },
    }
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.example.test/users/u-1").mock(
            return_value=httpx.Response(200, json={"id": "u-1"})
        )
        results = await run_operations(
            [operation],
            base_url="https://api.example.test",
            resolved_document=spec,
        )
    assert results[0]["status"] == "passed"
    assert results[0]["observed_schema"]["200"]["schema"]["properties"]["id"]["type"] == "string"


async def test_run_rejects_cross_host_redirect() -> None:
    operation = {
        "operation_id": "redirect",
        "method": "GET",
        "path": "/",
        "parameters": [],
        "responses": {},
    }
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.example.test/").mock(
            return_value=httpx.Response(302, headers={"location": "https://other.example.test/"})
        )
        results = await run_operations(
            [operation], base_url="https://api.example.test", resolved_document={}
        )
    assert results[0]["error_code"] == "redirect_blocked"


async def test_run_rejects_redirect_to_different_port() -> None:
    operation = {
        "operation_id": "redirect",
        "method": "GET",
        "path": "/",
        "parameters": [],
        "responses": {},
    }
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.example.test/").mock(
            return_value=httpx.Response(
                302, headers={"location": "https://api.example.test:444/target"}
            )
        )
        results = await run_operations(
            [operation], base_url="https://api.example.test", resolved_document={}
        )
    assert results[0]["error_code"] == "redirect_blocked"


async def test_run_stops_at_response_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_response_bytes", 4)
    operation = {
        "operation_id": "large",
        "method": "GET",
        "path": "/",
        "parameters": [],
        "responses": {},
    }
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.example.test/").mock(
            return_value=httpx.Response(200, content=b"12345")
        )
        results = await run_operations(
            [operation], base_url="https://api.example.test", resolved_document={}
        )
    assert results[0]["error_code"] == "response_too_large"
