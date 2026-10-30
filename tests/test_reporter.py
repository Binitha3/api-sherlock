from __future__ import annotations

import json

from app.services.differ import Change
from app.services.reporter import (
    build_report,
    exceeded_threshold,
    latency_stats,
    observations_from_results,
    render_html,
    render_markdown,
    render_report,
)

RESULTS = [
    {
        "operation_id": "listUsers",
        "method": "GET",
        "path": "/users",
        "status": "passed",
        "http_status": 200,
        "latency_ms": 10.0,
        "content_type": "application/json",
        "violations": [],
        "auth_hint": None,
        "error_code": None,
        "error_message": None,
        "observed_schema": {
            "200": {"content_type": "application/json", "schema": {"type": "array"}}
        },
    },
    {
        "operation_id": "getUser",
        "method": "GET",
        "path": "/users/{id}",
        "status": "failed",
        "http_status": 200,
        "latency_ms": 40.0,
        "content_type": "application/json",
        "violations": [
            {
                "path": "body.name",
                "message": "Missing required property 'name'.",
                "expected": "required",
                "actual": "missing",
            }
        ],
        "auth_hint": None,
        "error_code": None,
        "error_message": None,
        "observed_schema": {
            "200": {"content_type": "application/json", "schema": {"type": "object"}}
        },
    },
    {
        "operation_id": "getAccount",
        "method": "GET",
        "path": "/account",
        "status": "failed",
        "http_status": 401,
        "latency_ms": 20.0,
        "content_type": "application/json",
        "violations": [
            {
                "path": "status",
                "message": "Unexpected status code.",
                "expected": "200",
                "actual": "401",
            }
        ],
        "auth_hint": "The spec requires authentication but no credentials were sent.",
        "error_code": None,
        "error_message": None,
        "observed_schema": None,
    },
    {
        "operation_id": "createUser",
        "method": "POST",
        "path": "/users",
        "status": "skipped",
        "http_status": None,
        "latency_ms": None,
        "content_type": None,
        "violations": [],
        "auth_hint": None,
        "error_code": "request_body",
        "error_message": "The operation requires a request body.",
        "observed_schema": None,
    },
]

RUN = {
    "id": "run-1",
    "project_id": "project-1",
    "spec_id": "spec-1",
    "environment_id": "env-1",
    "base_url": "https://api.example.test",
    "status": "completed",
    "created_at": "2026-01-01T00:00:00+00:00",
    "finished_at": "2026-01-01T00:00:01+00:00",
}

CHANGES = [
    Change(
        "GET /users/{id} -> 200 -> body.name",
        "field_removed",
        "breaking",
        "string",
        None,
        "GET /users/{id} -> 200 -> body.name: field removed (was string).",
    ),
    Change(
        "GET /users -> 200 -> body.items[].plan",
        "field_added",
        "non_breaking",
        None,
        "string",
        "GET /users -> 200 -> body.items[].plan: field added.",
    ),
]


def test_latency_stats_uses_measured_responses_only() -> None:
    stats = latency_stats(RESULTS)
    assert stats["count"] == 3
    assert stats["min_ms"] == 10.0
    assert stats["max_ms"] == 40.0
    assert stats["mean_ms"] == 23.3
    assert stats["p95_ms"] == 40.0
    assert latency_stats([])["mean_ms"] is None


def test_observations_skip_operations_without_a_response() -> None:
    observations = observations_from_results(RESULTS)
    assert set(observations) == {"GET /users", "GET /users/{id}"}


def test_report_summarises_operations_changes_and_auth_hints() -> None:
    report = build_report(run=RUN, results=RESULTS, changes=CHANGES)
    assert report["summary"]["operations"] == {
        "total": 4,
        "passed": 1,
        "failed": 2,
        "skipped": 1,
    }
    assert report["summary"]["changes"] == {
        "total": 2,
        "breaking": 1,
        "warning": 0,
        "non_breaking": 1,
        "highest_severity": "breaking",
    }
    assert report["summary"]["breaking_change_paths"] == ["GET /users/{id} -> 200 -> body.name"]
    assert report["failed_operations"][1]["auth_hint"].startswith(
        "The spec requires authentication"
    )
    assert report["skipped_operations"][0]["reason"] == "The operation requires a request body."
    assert report["latency"]["count"] == 3
    json.dumps(report)  # serialisable


def test_markdown_report_contains_paths_hints_and_latency() -> None:
    report = build_report(run=RUN, results=RESULTS, changes=CHANGES)
    markdown = render_markdown(report)
    assert markdown.startswith("# API Sherlock report")
    assert "GET /users/{id} -> 200 -> body.name" in markdown
    assert "Auth hint: The spec requires authentication" in markdown
    assert "| p95 | 40.0 ms |" in markdown
    assert "## Skipped operations (1)" in markdown
    assert "None (contract changes not compared)" in markdown


def test_markdown_escapes_table_separators_in_messages() -> None:
    change = Change(
        "GET /x -> 200 -> body.a",
        "type_changed",
        "breaking",
        "string",
        "integer",
        "type changed from string | integer",
    )
    markdown = render_markdown(build_report(run=RUN, results=[], changes=[change]))
    assert "string \\| integer" in markdown


def test_html_report_escapes_untrusted_text() -> None:
    change = Change(
        "GET /x -> 200 -> body.a",
        "field_removed",
        "breaking",
        "string",
        None,
        "<script>alert(1)</script>",
    )
    rendered = render_html(build_report(run=RUN, results=[], changes=[change]))
    assert rendered.startswith("<!DOCTYPE html>")
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "GET /x -&gt; 200 -&gt; body.a" in rendered


def test_render_report_rejects_unknown_format() -> None:
    report = build_report(run=RUN, results=[])
    try:
        render_report(report, "pdf")
    except ValueError as exc:
        assert "pdf" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_fail_threshold_includes_higher_severities() -> None:
    non_breaking = [change for change in CHANGES if change.severity == "non_breaking"]
    warning = Change("p", "format_changed", "warning", "date", "date-time", "format changed")

    assert exceeded_threshold(CHANGES, "breaking")
    assert exceeded_threshold(CHANGES, "warning")
    assert exceeded_threshold(CHANGES, "non_breaking")
    assert exceeded_threshold(non_breaking, "warning") is False
    assert exceeded_threshold(non_breaking, "non_breaking")
    assert exceeded_threshold([warning], "warning")
    assert exceeded_threshold([warning], "breaking") is False
    assert exceeded_threshold([], "non_breaking") is False
