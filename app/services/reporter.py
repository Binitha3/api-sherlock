"""Build and render run reports as JSON, Markdown or HTML.

A report contains only contract metadata: operation names, status codes,
violations (field paths, expected and actual types), auth hints and latency
statistics. Response bodies and credentials are never part of a report.
"""

from __future__ import annotations

import html as html_lib
import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from app.services.differ import Change

ReportFormat = Literal["json", "markdown", "html"]
REPORT_FORMATS: tuple[str, ...] = ("json", "markdown", "html")

SEVERITY_WEIGHT = {"non_breaking": 0, "warning": 1, "breaking": 2}
SEVERITY_LABELS = {
    "breaking": "Breaking",
    "warning": "Warning",
    "non_breaking": "Non-breaking",
}
FAIL_ON_LEVELS: tuple[str, ...] = ("breaking", "warning", "non_breaking")


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def observations_from_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Map `METHOD path` to observed response shapes, skipping empty results."""
    observations: dict[str, Any] = {}
    for result in results:
        observed = result.get("observed_schema")
        if not observed:
            continue
        key = f"{result.get('method')} {result.get('path')}"
        observations[key] = observed
    return observations


# ---------------------------------------------------------------------------
# Report data
# ---------------------------------------------------------------------------


def latency_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Latency statistics over operations that returned a response."""
    values = sorted(
        float(result["latency_ms"]) for result in results if result.get("latency_ms") is not None
    )
    if not values:
        return {
            "count": 0,
            "min_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "p95_ms": None,
        }
    index = max(0, math.ceil(0.95 * len(values)) - 1)
    return {
        "count": len(values),
        "min_ms": round(values[0], 1),
        "max_ms": round(values[-1], 1),
        "mean_ms": round(sum(values) / len(values), 1),
        "p95_ms": round(values[index], 1),
    }


def change_summary(changes: list[Change]) -> dict[str, Any]:
    counts = {
        "total": len(changes),
        "breaking": sum(change.severity == "breaking" for change in changes),
        "warning": sum(change.severity == "warning" for change in changes),
        "non_breaking": sum(change.severity == "non_breaking" for change in changes),
    }
    highest = max(
        (change.severity for change in changes),
        key=lambda severity: SEVERITY_WEIGHT[severity],
        default=None,
    )
    return {**counts, "highest_severity": highest}


def exceeded_threshold(changes: list[Change], fail_on: str) -> bool:
    """True when any change is at or above the `fail_on` severity."""
    threshold = SEVERITY_WEIGHT[fail_on]
    return any(SEVERITY_WEIGHT[change.severity] >= threshold for change in changes)


def build_report(
    *,
    run: dict[str, Any],
    results: list[dict[str, Any]],
    changes: list[Change] | None = None,
    baseline: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble a serialisable report from a run and its contract changes."""
    changes = changes or []
    checked = [change for change in changes if change.severity == "breaking"]
    failed = [
        {
            "operation_id": result.get("operation_id"),
            "method": result.get("method"),
            "path": result.get("path"),
            "http_status": result.get("http_status"),
            "auth_hint": result.get("auth_hint"),
            "violations": result.get("violations") or [],
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
        }
        for result in results
        if result.get("status") == "failed"
    ]
    skipped = [
        {
            "operation_id": result.get("operation_id"),
            "method": result.get("method"),
            "path": result.get("path"),
            "reason": result.get("error_message") or "Not run.",
        }
        for result in results
        if result.get("status") == "skipped"
    ]

    return {
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "run": {
            "id": run.get("id"),
            "project_id": run.get("project_id"),
            "spec_id": run.get("spec_id"),
            "environment_id": run.get("environment_id"),
            "base_url": run.get("base_url"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "finished_at": run.get("finished_at"),
        },
        "baseline": baseline,
        "summary": {
            "operations": {
                "total": len(results),
                "passed": sum(result.get("status") == "passed" for result in results),
                "failed": len(failed),
                "skipped": len(skipped),
            },
            "changes": change_summary(changes),
            "breaking_change_paths": [change.path for change in checked],
        },
        "changes": [change.to_dict() for change in changes],
        "operations": [
            {
                "operation_id": result.get("operation_id"),
                "method": result.get("method"),
                "path": result.get("path"),
                "status": result.get("status"),
                "http_status": result.get("http_status"),
                "latency_ms": result.get("latency_ms"),
                "violation_count": len(result.get("violations") or []),
                "auth_hint": result.get("auth_hint"),
            }
            for result in results
        ],
        "failed_operations": failed,
        "skipped_operations": skipped,
        "latency": latency_stats(results),
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md(value: Any) -> str:
    """Render a table-safe Markdown cell."""
    if value is None:
        return "—"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text.strip() or "—"


def _md_code(value: Any) -> str:
    text = _md(value)
    if text == "—":
        return text
    return f"`{text.replace('`', '')}`"


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md(cell) for cell in row) + " |")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    operations = report["summary"]["operations"]
    changes_summary = report["summary"]["changes"]
    latency = report["latency"]
    baseline = report.get("baseline")

    lines: list[str] = [
        "# API Sherlock report",
        "",
        _md_table(
            ["Run", "Value"],
            [
                ["Run ID", _md_code(run.get("id"))],
                ["Base URL", _md_code(run.get("base_url"))],
                ["Status", run.get("status")],
                ["Finished", run.get("finished_at") or run.get("created_at")],
                ["Generated", report["generated_at"]],
                [
                    "Baseline",
                    f"{baseline.get('label')} (`{baseline.get('id')}`)"
                    if baseline
                    else "None (contract changes not compared)",
                ],
            ],
        ),
        "",
        "## Summary",
        "",
        _md_table(
            ["Operations", "Count"],
            [
                ["Total", operations["total"]],
                ["Passed", operations["passed"]],
                ["Failed", operations["failed"]],
                ["Skipped", operations["skipped"]],
            ],
        ),
        "",
        _md_table(
            ["Contract changes", "Count"],
            [
                ["Total", changes_summary["total"]],
                ["Breaking", changes_summary["breaking"]],
                ["Warning", changes_summary["warning"]],
                ["Non-breaking", changes_summary["non_breaking"]],
            ],
        ),
        "",
    ]

    if report["changes"]:
        lines.extend(
            [
                "## Contract changes",
                "",
                _md_table(
                    ["Severity", "Path", "Change"],
                    [
                        [
                            SEVERITY_LABELS[change["severity"]],
                            _md_code(change["path"]),
                            change["message"],
                        ]
                        for change in report["changes"]
                    ],
                ),
                "",
            ]
        )

    lines.append(f"## Failed operations ({len(report['failed_operations'])})")
    lines.append("")
    if not report["failed_operations"]:
        lines.extend(["No operation failed contract validation.", ""])
    for item in report["failed_operations"]:
        lines.append(
            f"### {_md(item.get('method'))} {_md(item.get('path'))} — "
            f"HTTP {_md(item.get('http_status'))}"
        )
        lines.append("")
        lines.append(f"- Operation: {_md_code(item.get('operation_id'))}")
        if item.get("auth_hint"):
            lines.append(f"- Auth hint: {_md(item['auth_hint'])}")
        for violation in item["violations"]:
            detail = (
                f"- `{_md(violation.get('path'))}` — {_md(violation.get('message'))}"
                f" (expected: {_md(violation.get('expected'))}, "
                f"actual: {_md(violation.get('actual'))})"
            )
            lines.append(detail)
        if not item["violations"] and not item.get("auth_hint"):
            lines.append(f"- {_md(item.get('error_message') or 'Request failed.')}")
        lines.append("")

    if report["skipped_operations"]:
        lines.extend(
            [
                f"## Skipped operations ({len(report['skipped_operations'])})",
                "",
                _md_table(
                    ["Operation", "Path", "Reason"],
                    [
                        [item.get("operation_id"), item.get("path"), item.get("reason")]
                        for item in report["skipped_operations"]
                    ],
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## Latency",
            "",
            _md_table(
                ["Metric", "Value"],
                [
                    ["Measured responses", latency["count"]],
                    ["Min", _ms(latency["min_ms"])],
                    ["Mean", _ms(latency["mean_ms"])],
                    ["p95", _ms(latency["p95_ms"])],
                    ["Max", _ms(latency["max_ms"])],
                ],
            ),
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _ms(value: Any) -> str:
    return "—" if value is None else f"{value} ms"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    return html_lib.escape("—" if value is None else str(value))


def _html_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: dict[str, Any]) -> str:
    run = report["run"]
    operations = report["summary"]["operations"]
    changes_summary = report["summary"]["changes"]
    latency = report["latency"]
    baseline = report.get("baseline")

    failed_blocks: list[str] = []
    if not report["failed_operations"]:
        failed_blocks.append("<p>No operation failed contract validation.</p>")
    for item in report["failed_operations"]:
        rows = []
        if item.get("auth_hint"):
            rows.append(
                {
                    "path": "auth",
                    "message": item["auth_hint"],
                    "expected": "",
                    "actual": "",
                }
            )
        rows.extend(item["violations"])
        if not rows:
            rows.append(
                {
                    "path": "request",
                    "message": item.get("error_message") or "Request failed.",
                    "expected": "",
                    "actual": "",
                }
            )
        failed_blocks.append(
            '<section class="op">'
            f"<h3>{_esc(item.get('method'))} {_esc(item.get('path'))} "
            f'<span class="muted">HTTP {_esc(item.get("http_status"))}</span></h3>'
            f'<p class="muted">operation: {_esc(item.get("operation_id"))}</p>'
            + _html_table(
                ["Path", "Message", "Expected", "Actual"],
                [
                    [row.get("path"), row.get("message"), row.get("expected"), row.get("actual")]
                    for row in rows
                ],
            )
            + "</section>"
        )

    changes_html = (
        _html_table(
            ["Severity", "Path", "Change"],
            [
                [
                    SEVERITY_LABELS[change["severity"]],
                    change["path"],
                    change["message"],
                ]
                for change in report["changes"]
            ],
        )
        if report["changes"]
        else "<p>No contract changes were detected.</p>"
    )

    skipped_html = (
        _html_table(
            ["Operation", "Path", "Reason"],
            [
                [item.get("operation_id"), item.get("path"), item.get("reason")]
                for item in report["skipped_operations"]
            ],
        )
        if report["skipped_operations"]
        else "<p>No operations were skipped.</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API Sherlock report — {_esc(run.get("id"))}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 32px; background: #f7f7f8; color: #16161a;
         font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif; line-height: 1.5; }}
  main {{ max-width: 960px; margin: 0 auto; background: #fff; padding: 32px;
          border-radius: 12px; border: 1px solid #e4e4e7; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 32px 0 12px; }}
  h3 {{ font-size: 14px; margin: 16px 0 6px; }}
  .muted {{ color: #6b7280; font-weight: 400; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee;
            vertical-align: top; }}
  th {{ background: #fafafa; font-weight: 600; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
  .op {{ border-top: 1px solid #eee; padding-top: 8px; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 999px; background: #f4f4f5;
            font-size: 12px; }}
</style>
</head>
<body>
<main>
  <h1>API Sherlock report</h1>
  <p class="muted">Generated {_esc(report["generated_at"])}</p>
  {
        _html_table(
            ["Run", "Value"],
            [
                ["Run ID", run.get("id")],
                ["Base URL", run.get("base_url")],
                ["Status", run.get("status")],
                ["Finished", run.get("finished_at") or run.get("created_at")],
                [
                    "Baseline",
                    f"{baseline.get('label')} ({baseline.get('id')})"
                    if baseline
                    else "None (contract changes not compared)",
                ],
            ],
        )
    }
  <h2>Summary</h2>
  {
        _html_table(
            ["Operations", "Count"],
            [
                ["Total", operations["total"]],
                ["Passed", operations["passed"]],
                ["Failed", operations["failed"]],
                ["Skipped", operations["skipped"]],
            ],
        )
    }
  {
        _html_table(
            ["Contract changes", "Count"],
            [
                ["Total", changes_summary["total"]],
                ["Breaking", changes_summary["breaking"]],
                ["Warning", changes_summary["warning"]],
                ["Non-breaking", changes_summary["non_breaking"]],
            ],
        )
    }
  <h2>Contract changes</h2>
  {changes_html}
  <h2>Failed operations ({len(report["failed_operations"])})</h2>
  {"".join(failed_blocks)}
  <h2>Skipped operations</h2>
  {skipped_html}
  <h2>Latency</h2>
  {
        _html_table(
            ["Metric", "Value"],
            [
                ["Measured responses", latency["count"]],
                ["Min", _ms(latency["min_ms"])],
                ["Mean", _ms(latency["mean_ms"])],
                ["p95", _ms(latency["p95_ms"])],
                ["Max", _ms(latency["max_ms"])],
            ],
        )
    }
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_report(report: dict[str, Any], report_format: str) -> str:
    """Render a report in the requested format; unknown formats raise ValueError."""
    if report_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False)
    if report_format == "markdown":
        return render_markdown(report)
    if report_format == "html":
        return render_html(report)
    raise ValueError(f"Unsupported report format '{report_format}'.")


def media_type(report_format: str) -> str:
    return {
        "json": "application/json",
        "markdown": "text/markdown; charset=utf-8",
        "html": "text/html; charset=utf-8",
    }[report_format]
