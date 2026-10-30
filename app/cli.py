"""`sherlock` CLI: run a contract check in CI without the server or database.

Local mode only. Credentials come from environment variables and are never
written to a file, a report or a log.

Exit codes: 0 = clean, 1 = changes at or above --fail-on, 2 = tool/config error.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import typer

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.differ import Change, diff_observed
from app.services.reporter import (
    FAIL_ON_LEVELS,
    REPORT_FORMATS,
    SEVERITY_LABELS,
    build_report,
    exceeded_threshold,
    observations_from_results,
    render_report,
)
from app.services.runner import AuthContext, run_operations
from app.services.spec_import import import_document

BASELINE_VERSION = 1

app = typer.Typer(
    add_completion=False,
    help="Find exactly what changed in an API contract before it breaks an integration.",
    no_args_is_help=True,
)
baseline_app = typer.Typer(help="Save a baseline from live API responses.", no_args_is_help=True)
app.add_typer(baseline_app, name="baseline")


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=2)


def _ok(message: str) -> None:
    typer.echo(message, err=True)


def _allow_private_hosts(enabled: bool) -> None:
    """Local development escape hatch, mirroring the ALLOW_PRIVATE_HOSTS setting."""
    if not enabled:
        return
    os.environ["ALLOW_PRIVATE_HOSTS"] = "true"
    get_settings.cache_clear()


def _auth_context(auth_type: str) -> AuthContext:
    """Build credentials from the environment; never accept them as arguments."""
    normalized = auth_type.strip().lower()
    if normalized not in {"none", "bearer", "api_key_header", "basic"}:
        _fail(f"unknown auth type '{auth_type}'. Use none, bearer, api_key_header or basic.")
    if normalized == "bearer":
        token = os.getenv("SHERLOCK_TOKEN")
        if not token:
            _fail("bearer auth selected but SHERLOCK_TOKEN is not set.")
        return AuthContext(auth_type="bearer", token=token)
    if normalized == "api_key_header":
        api_key = os.getenv("SHERLOCK_API_KEY")
        if not api_key:
            _fail("api_key_header auth selected but SHERLOCK_API_KEY is not set.")
        header = os.getenv("SHERLOCK_AUTH_HEADER", "X-API-Key")
        return AuthContext(auth_type="api_key_header", api_key=api_key, header_name=header)
    if normalized == "basic":
        username = os.getenv("SHERLOCK_USERNAME")
        if username is None:
            _fail("basic auth selected but SHERLOCK_USERNAME is not set.")
        return AuthContext(
            auth_type="basic", username=username, password=os.getenv("SHERLOCK_PASSWORD", "")
        )
    return AuthContext()


def _execute(
    *,
    spec_path: Path,
    base_url: str,
    auth_type: str,
    allow_private_hosts: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Import the spec and run every inferable operation against `base_url`."""
    _allow_private_hosts(allow_private_hosts)
    try:
        raw = spec_path.read_bytes()
        imported = import_document(raw, filename=spec_path.name)
        auth = _auth_context(auth_type)
        results = asyncio.run(
            run_operations(
                imported.operations,
                base_url=base_url,
                resolved_document=imported.resolved_document,
                auth=auth,
            )
        )
    except AppError as exc:
        _fail(exc.message)
    except OSError as exc:
        _fail(f"could not read spec file '{spec_path}': {exc.strerror or exc}")
    meta = {
        "title": imported.title,
        "version": imported.spec_version,
        "openapi": imported.openapi_version,
    }
    return imported.operations, results, meta


def _load_baseline(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"could not read baseline file '{path}': {exc.strerror or exc}")
    except json.JSONDecodeError as exc:
        _fail(f"baseline file '{path}' is not valid JSON: {exc.msg} (line {exc.lineno}).")
    if not isinstance(payload, dict):
        _fail(f"baseline file '{path}' must contain a JSON object.")
    observations = payload.get("operations", payload)
    if not isinstance(observations, dict):
        _fail(f"baseline file '{path}' has no 'operations' object.")
    for key, value in observations.items():
        if not isinstance(value, dict):
            _fail(f"baseline file '{path}' has an invalid entry for '{key}'.")
    return observations


def _baseline_payload(
    *, base_url: str, meta: dict[str, Any], observations: dict[str, Any]
) -> dict[str, Any]:
    return {
        "version": BASELINE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "spec": {
            "title": meta.get("title"),
            "version": meta.get("version"),
            "openapi": meta.get("openapi"),
        },
        "operations": observations,
    }


def _run_label(results: list[dict[str, Any]]) -> str:
    passed = sum(result.get("status") == "passed" for result in results)
    failed = sum(result.get("status") == "failed" for result in results)
    skipped = sum(result.get("status") == "skipped" for result in results)
    return f"{len(results)} operation(s): {passed} passed, {failed} failed, {skipped} skipped"


def _severity_counts(changes: list[Change]) -> str:
    if not changes:
        return "0 contract changes"
    parts = [
        f"{sum(change.severity == severity for change in changes)} "
        f"{SEVERITY_LABELS[severity].lower()}"
        for severity in ("breaking", "warning", "non_breaking")
    ]
    return f"{len(changes)} contract change(s): " + ", ".join(parts)


@app.command("check")
def check(
    spec: Path = typer.Option(
        ...,
        "--spec",
        exists=True,
        dir_okay=False,
        readable=True,
        help="OpenAPI 3 file (JSON/YAML).",
    ),
    base_url: str = typer.Option(..., "--base-url", help="Live API base URL."),
    baseline: Path | None = typer.Option(
        None, "--baseline", help="Baseline JSON written by `sherlock baseline save`."
    ),
    fail_on: str = typer.Option(
        "breaking",
        "--fail-on",
        help="Lowest severity that fails the build: breaking, warning or non_breaking.",
    ),
    report_format: str = typer.Option(
        "markdown", "--format", "-f", help="Report format: markdown, json or html."
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the report to a file."),
    auth_type: str = typer.Option(
        "none",
        "--auth-type",
        envvar="SHERLOCK_AUTH_TYPE",
        help="none, bearer, api_key_header or basic. Secrets come from SHERLOCK_* env vars.",
    ),
    allow_private_hosts: bool = typer.Option(
        False,
        "--allow-private-hosts",
        help="Allow loopback/private targets for local development only.",
    ),
) -> None:
    """Run every inferable operation and report contract changes."""
    normalized_format = report_format.strip().lower()
    if normalized_format not in REPORT_FORMATS:
        _fail(f"unknown format '{report_format}'. Use markdown, json or html.")
    normalized_fail_on = fail_on.strip().lower()
    if normalized_fail_on not in FAIL_ON_LEVELS:
        _fail(f"unknown --fail-on level '{fail_on}'. Use breaking, warning or non_breaking.")

    _, results, meta = _execute(
        spec_path=spec,
        base_url=base_url,
        auth_type=auth_type,
        allow_private_hosts=allow_private_hosts,
    )
    observations = observations_from_results(results)

    changes: list[Change] = []
    baseline_meta: dict[str, Any] | None = None
    if baseline is not None:
        baseline_observations = _load_baseline(baseline)
        changes = diff_observed(baseline_observations, observations)
        baseline_meta = {
            "id": str(baseline),
            "label": baseline.name,
            "run_id": None,
            "created_at": None,
        }

    report = build_report(
        run={
            "id": None,
            "project_id": None,
            "spec_id": None,
            "environment_id": None,
            "base_url": base_url,
            "status": "completed",
            "created_at": datetime.now(UTC).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        },
        results=results,
        changes=changes,
        baseline=baseline_meta,
    )
    report["spec"] = meta

    text = render_report(report, normalized_format)
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        except OSError as exc:
            _fail(f"could not write report to '{output}': {exc.strerror or exc}")
    else:
        typer.echo(text)

    _ok(_run_label(results))
    _ok(_severity_counts(changes))
    if exceeded_threshold(changes, normalized_fail_on):
        _ok(f"FAILED: changes at or above '{normalized_fail_on}' severity.")
        raise typer.Exit(code=1)
    _ok(f"OK: no changes at or above '{normalized_fail_on}' severity.")
    raise typer.Exit(code=0)


@baseline_app.command("save")
def baseline_save(
    spec: Path = typer.Option(
        ...,
        "--spec",
        exists=True,
        dir_okay=False,
        readable=True,
        help="OpenAPI 3 file (JSON/YAML).",
    ),
    base_url: str = typer.Option(..., "--base-url", help="Live API base URL."),
    output: Path = typer.Option(
        Path("baseline.json"), "--output", "-o", help="Where to write the baseline JSON."
    ),
    auth_type: str = typer.Option(
        "none",
        "--auth-type",
        envvar="SHERLOCK_AUTH_TYPE",
        help="none, bearer, api_key_header or basic. Secrets come from SHERLOCK_* env vars.",
    ),
    allow_private_hosts: bool = typer.Option(
        False,
        "--allow-private-hosts",
        help="Allow loopback/private targets for local development only.",
    ),
) -> None:
    """Save observed response shapes (never response bodies) as a baseline."""
    _, results, meta = _execute(
        spec_path=spec,
        base_url=base_url,
        auth_type=auth_type,
        allow_private_hosts=allow_private_hosts,
    )
    observations = observations_from_results(results)
    if not observations:
        _ok(_run_label(results))
        _fail("no operation produced a response; nothing was saved.")
    payload = _baseline_payload(base_url=base_url, meta=meta, observations=observations)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _fail(f"could not write baseline to '{output}': {exc.strerror or exc}")
    _ok(_run_label(results))
    typer.echo(f"Saved baseline for {len(observations)} operation(s) to {output}")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
