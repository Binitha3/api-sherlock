"""Phase 1 API routes: health, projects, spec import/listing."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError, SpecInvalidError
from app.db.models import Baseline, Environment, Project, Run, RunResult, Spec
from app.db.session import get_db
from app.services.differ import Change, diff_observed, diff_operations
from app.services.reporter import (
    build_report,
    media_type,
    observations_from_results,
    render_report,
)
from app.services.runner import AuthContext, run_operations
from app.services.spec_import import fetch_document, import_document

router = APIRouter()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def project_to_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": _iso(project.created_at),
        "spec_count": len(project.specs),
    }


def spec_to_dict(spec: Spec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "project_id": spec.project_id,
        "version_label": spec.version_label,
        "openapi_version": spec.openapi_version,
        "content_hash": spec.content_hash,
        "source_url": spec.source_url,
        "created_at": _iso(spec.created_at),
        "operation_count": len(spec.operations or []),
        "operations": spec.operations or [],
    }


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project '{project_id}' not found.")
    return project


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-sherlock", "version": __version__}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


@router.post("/projects", status_code=201, tags=["projects"])
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = Project(name=payload.name, description=payload.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project_to_dict(project)


@router.get("/projects", tags=["projects"])
def list_projects(db: Session = Depends(get_db)) -> dict[str, Any]:
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return {"projects": [project_to_dict(p) for p in projects]}


# ---------------------------------------------------------------------------
# Spec import / listing
# ---------------------------------------------------------------------------


def _version_label(raw: Any) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:120]
    return ""


@router.post("/projects/{project_id}/specs", status_code=201, tags=["specs"])
async def create_spec(
    project_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Import an OpenAPI document: multipart file upload or JSON {\"url\": ...}."""
    project = _get_project(db, project_id)
    settings = get_settings()
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

    if content_type == "application/json":
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 — malformed JSON body
            raise AppError(
                'Request body must be JSON, e.g. {"url": "https://example.com/openapi.json"}.',
                code="request_invalid",
                status_code=400,
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            raise AppError(
                "JSON body must include a 'url' string.",
                code="request_invalid",
                status_code=400,
            )
        source_url = body["url"].strip()
        if not source_url:
            raise AppError(
                "JSON body 'url' must not be empty.", code="request_invalid", status_code=400
            )
        raw, fetch_content_type = await fetch_document(source_url)
        filename = source_url.rsplit("/", 1)[-1] or None
        label = _version_label(body.get("version_label"))

    elif content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise AppError(
                "multipart/form-data upload requires a 'file' field.",
                code="request_invalid",
                status_code=400,
            )
        raw = await upload.read(settings.max_spec_bytes + 1)
        if len(raw) > settings.max_spec_bytes:
            raise SpecInvalidError(
                f"Spec document exceeds the {settings.max_spec_bytes // (1024 * 1024)} MiB limit."
            )
        filename = getattr(upload, "filename", None) or None
        fetch_content_type = getattr(upload, "content_type", None)
        source_url = None
        label = _version_label(form.get("version_label"))

    else:
        raise AppError(
            "Unsupported content type. Send multipart/form-data with a 'file' field, "
            "or application/json with a 'url' field.",
            code="unsupported_media_type",
            status_code=415,
        )

    result = import_document(raw, filename=filename, content_type=fetch_content_type)

    spec = Spec(
        project_id=project.id,
        version_label=label or result.spec_version or "draft",
        openapi_version=result.openapi_version,
        content_hash=result.content_hash,
        raw_document=result.raw_document,
        resolved_document=result.resolved_document,
        operations=result.operations,
        source_url=source_url,
    )
    db.add(spec)
    db.commit()
    db.refresh(spec)
    return spec_to_dict(spec)


@router.get("/projects/{project_id}/specs", tags=["specs"])
def list_specs(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _get_project(db, project_id)
    specs = sorted(project.specs, key=lambda s: s.created_at, reverse=True)
    return {"specs": [spec_to_dict(s) for s in specs]}


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


AUTH_TYPES = {"none", "bearer", "api_key_header", "basic"}


class EnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=2000)
    auth_type: Literal["none", "bearer", "api_key_header", "basic"] = "none"
    auth_header_name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_environment_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an absolute http or https URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials")
        return value.strip().rstrip("/")

    @field_validator("auth_header_name")
    @classmethod
    def _validate_auth_header(cls, value: str | None) -> str | None:
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("auth_header_name must not contain line breaks")
        return value.strip() if value else None


class EnvironmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    auth_type: Literal["none", "bearer", "api_key_header", "basic"] | None = None
    auth_header_name: str | None = Field(default=None, min_length=1, max_length=200)


class RunAuth(BaseModel):
    """Run-only auth input. These values are intentionally never persisted."""

    token: SecretStr | None = None
    api_key: SecretStr | None = None
    username: SecretStr | None = None
    password: SecretStr | None = None


class RunCreate(BaseModel):
    spec_id: str
    environment_id: str
    include_operations: list[str] | None = None
    parameter_values: dict[str, dict[str, Any]] | None = None
    auth: RunAuth | None = None


def environment_to_dict(environment: Environment) -> dict[str, Any]:
    return {
        "id": environment.id,
        "project_id": environment.project_id,
        "name": environment.name,
        "base_url": environment.base_url,
        "auth_type": environment.auth_type,
        "auth_header_name": environment.auth_header_name,
        "created_at": _iso(environment.created_at),
    }


def _get_environment(db: Session, project_id: str, environment_id: str) -> Environment:
    environment = db.scalar(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.project_id == project_id,
        )
    )
    if environment is None:
        raise NotFoundError(f"Environment '{environment_id}' not found.")
    return environment


def _clean_environment_update(payload: EnvironmentUpdate) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        name = changes["name"].strip()
        if not name:
            raise AppError("Environment name must not be blank.", code="request_invalid")
        changes["name"] = name
    if "base_url" in changes and changes["base_url"] is not None:
        changes["base_url"] = EnvironmentCreate._validate_base_url(changes["base_url"])
    if "auth_header_name" in changes and changes["auth_header_name"] is not None:
        changes["auth_header_name"] = EnvironmentCreate._validate_auth_header(
            changes["auth_header_name"]
        )
    return changes


@router.post("/projects/{project_id}/environments", status_code=201, tags=["environments"])
def create_environment(
    project_id: str, payload: EnvironmentCreate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    project = _get_project(db, project_id)
    environment = Environment(project_id=project.id, **payload.model_dump())
    db.add(environment)
    db.commit()
    db.refresh(environment)
    return environment_to_dict(environment)


@router.get("/projects/{project_id}/environments", tags=["environments"])
def list_environments(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _get_project(db, project_id)
    environments = db.scalars(
        select(Environment)
        .where(Environment.project_id == project_id)
        .order_by(Environment.created_at.desc())
    ).all()
    return {"environments": [environment_to_dict(item) for item in environments]}


@router.get("/projects/{project_id}/environments/{environment_id}", tags=["environments"])
def get_environment(
    project_id: str, environment_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    _get_project(db, project_id)
    return environment_to_dict(_get_environment(db, project_id, environment_id))


@router.patch("/projects/{project_id}/environments/{environment_id}", tags=["environments"])
def update_environment(
    project_id: str,
    environment_id: str,
    payload: EnvironmentUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_project(db, project_id)
    environment = _get_environment(db, project_id, environment_id)
    changes = _clean_environment_update(payload)
    if not changes:
        raise AppError("Provide at least one environment field to update.", code="request_invalid")
    for key, value in changes.items():
        setattr(environment, key, value)
    db.commit()
    db.refresh(environment)
    return environment_to_dict(environment)


@router.delete(
    "/projects/{project_id}/environments/{environment_id}",
    status_code=204,
    tags=["environments"],
)
def delete_environment(
    project_id: str, environment_id: str, db: Session = Depends(get_db)
) -> Response:
    _get_project(db, project_id)
    environment = _get_environment(db, project_id, environment_id)
    db.delete(environment)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Request runs
# ---------------------------------------------------------------------------


def _result_to_dict(result: RunResult) -> dict[str, Any]:
    return {
        "operation_id": result.operation_id,
        "method": result.method,
        "path": result.path,
        "status": result.status,
        "http_status": result.http_status,
        "latency_ms": result.latency_ms,
        "content_type": result.content_type,
        "violations": result.violations,
        "auth_hint": result.auth_hint,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "observed_schema": result.observed_schema,
    }


def _run_to_dict(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "spec_id": run.spec_id,
        "environment_id": run.environment_id,
        "base_url": run.base_url,
        "status": run.status,
        "summary": run.summary,
        "error_message": run.error_message,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "results": [_result_to_dict(result) for result in run.results],
    }


def _run_summary(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(result["status"] == "passed" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
    }


@router.post("/runs", status_code=201, tags=["runs"])
async def create_run(payload: RunCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    spec = db.get(Spec, payload.spec_id)
    environment = db.get(Environment, payload.environment_id)
    if spec is None:
        raise NotFoundError(f"Spec '{payload.spec_id}' not found.")
    if environment is None:
        raise NotFoundError(f"Environment '{payload.environment_id}' not found.")
    if spec.project_id != environment.project_id:
        raise AppError(
            "The spec and environment must belong to the same project.",
            code="run_invalid",
            status_code=422,
        )

    operations = spec.operations or []
    include = payload.include_operations
    if include:
        available = {str(operation.get("operation_id")) for operation in operations} | {
            f"{operation.get('method')} {operation.get('path')}" for operation in operations
        }
        unknown = sorted(set(include) - available)
        if unknown:
            raise AppError(
                f"Unknown operation selector: {unknown[0]}.", code="run_invalid", status_code=422
            )

    secret = payload.auth
    auth = AuthContext(
        auth_type=environment.auth_type,
        header_name=environment.auth_header_name,
        token=secret.token.get_secret_value() if secret and secret.token else None,
        api_key=secret.api_key.get_secret_value() if secret and secret.api_key else None,
        username=secret.username.get_secret_value() if secret and secret.username else None,
        password=secret.password.get_secret_value() if secret and secret.password else None,
    )

    run = Run(
        project_id=spec.project_id,
        spec_id=spec.id,
        environment_id=environment.id,
        base_url=environment.base_url,
        status="running",
        include_operations=include,
        parameter_values=payload.parameter_values,
        started_at=datetime.now().astimezone(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        results = await run_operations(
            operations,
            base_url=environment.base_url,
            resolved_document=spec.resolved_document,
            include=include,
            parameter_values=payload.parameter_values,
            auth=auth,
        )
    except AppError:
        db.delete(run)
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001 — persist safe failure without internals
        db.rollback()
        run = db.get(Run, run.id)
        if run is None:
            raise AppError(
                "Run could not be completed.", code="run_failed", status_code=502
            ) from exc
        run.status = "failed"
        run.error_message = "Run could not be completed due to an internal execution error."
        run.finished_at = datetime.now().astimezone()
        db.commit()
        return _run_to_dict(run)

    for result in results:
        db.add(
            RunResult(
                run_id=run.id,
                operation_id=str(result.get("operation_id") or ""),
                method=str(result.get("method") or "GET"),
                path=str(result.get("path") or "/"),
                status=result["status"],
                http_status=result["http_status"],
                latency_ms=result["latency_ms"],
                content_type=result["content_type"],
                violations=result["violations"],
                auth_hint=result["auth_hint"],
                error_code=result["error_code"],
                error_message=result["error_message"],
                observed_schema=result["observed_schema"],
            )
        )
    run.summary = _run_summary(results)
    run.status = "completed"
    run.finished_at = datetime.now().astimezone()
    db.commit()
    db.refresh(run)
    return _run_to_dict(run)


@router.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"Run '{run_id}' not found.")
    return _run_to_dict(run)


def _change_payload(changes: list[Change]) -> dict[str, Any]:
    return {
        "summary": {
            "total": len(changes),
            "breaking": sum(change.severity == "breaking" for change in changes),
            "warning": sum(change.severity == "warning" for change in changes),
            "non_breaking": sum(change.severity == "non_breaking" for change in changes),
        },
        "changes": [change.to_dict() for change in changes],
    }


def _get_run(db: Session, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"Run '{run_id}' not found.")
    return run


def _baseline_observations(run: Run) -> dict[str, Any]:
    return observations_from_results([_result_to_dict(result) for result in run.results])


class BaselineCreate(BaseModel):
    label: str = Field(default="baseline", min_length=1, max_length=120)

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("label must not be blank")
        return value


@router.post("/runs/{run_id}/baseline", status_code=201, tags=["baselines"])
def save_baseline(
    run_id: str,
    payload: BaselineCreate | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save only inferred response shapes, never response bodies or credentials."""
    run = _get_run(db, run_id)
    if run.status != "completed":
        raise AppError("Only completed runs can be saved as baselines.", code="baseline_invalid")
    observations = _baseline_observations(run)
    if not observations:
        raise AppError(
            "This run has no observed JSON response shapes to save.", code="baseline_empty"
        )
    baseline = Baseline(
        project_id=run.project_id,
        run_id=run.id,
        label=payload.label if payload else "baseline",
        data={"observations": observations},
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)
    return {
        "id": baseline.id,
        "project_id": baseline.project_id,
        "run_id": baseline.run_id,
        "label": baseline.label,
        "created_at": _iso(baseline.created_at),
        "operation_count": len(observations),
    }


@router.get("/projects/{project_id}/baselines", tags=["baselines"])
def list_baselines(project_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _get_project(db, project_id)
    baselines = db.scalars(
        select(Baseline)
        .where(Baseline.project_id == project_id)
        .order_by(Baseline.created_at.desc())
    ).all()
    return {
        "baselines": [
            {
                "id": baseline.id,
                "project_id": baseline.project_id,
                "run_id": baseline.run_id,
                "label": baseline.label,
                "created_at": _iso(baseline.created_at),
                "operation_count": len((baseline.data or {}).get("observations", {})),
            }
            for baseline in baselines
        ]
    }


@router.get("/runs/{run_id}/diff", tags=["diff"])
def diff_run_against_baseline(
    run_id: str, baseline_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    run = _get_run(db, run_id)
    if run.status != "completed":
        raise AppError("Only completed runs can be compared to a baseline.", code="run_invalid")
    baseline = db.get(Baseline, baseline_id)
    if baseline is None or baseline.project_id != run.project_id:
        raise NotFoundError(f"Baseline '{baseline_id}' not found.")
    old = (baseline.data or {}).get("observations") or {}
    new = _baseline_observations(run)
    return _change_payload(diff_observed(old, new))


class SpecsDiffRequest(BaseModel):
    old_spec_id: str
    new_spec_id: str


@router.post("/projects/{project_id}/diff/specs", tags=["diff"])
def diff_specs(
    project_id: str, payload: SpecsDiffRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    _get_project(db, project_id)
    old_spec = db.get(Spec, payload.old_spec_id)
    new_spec = db.get(Spec, payload.new_spec_id)
    if (
        old_spec is None
        or new_spec is None
        or old_spec.project_id != project_id
        or new_spec.project_id != project_id
    ):
        raise NotFoundError("One or both specs were not found in this project.")
    changes = diff_operations(
        old_spec.operations or [],
        new_spec.operations or [],
        old_document=old_spec.resolved_document,
        new_document=new_spec.resolved_document,
    )
    return _change_payload(changes)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _report_baseline(db: Session, run: Run, baseline_id: str | None) -> Baseline | None:
    """Explicit baseline, or the newest project baseline saved before this run."""
    if baseline_id is not None:
        baseline = db.get(Baseline, baseline_id)
        if baseline is None or baseline.project_id != run.project_id:
            raise NotFoundError(f"Baseline '{baseline_id}' not found.")
        return baseline
    return db.scalars(
        select(Baseline)
        .where(Baseline.project_id == run.project_id, Baseline.created_at <= run.created_at)
        .order_by(Baseline.created_at.desc())
        .limit(1)
    ).first()


@router.get("/runs/{run_id}/report", tags=["reports"])
def run_report(
    run_id: str,
    format: Literal["json", "markdown", "html"] = "json",
    baseline_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    """Report a completed run: validation results, auth hints, latencies and,
    when a baseline exists, its contract changes.

    Without `baseline_id`, the newest baseline saved before this run is used so
    a report never compares against a baseline created later.
    """
    run = _get_run(db, run_id)
    if run.status != "completed":
        raise AppError("Only completed runs can be reported.", code="run_invalid")

    results = [_result_to_dict(result) for result in run.results]
    baseline = _report_baseline(db, run, baseline_id)
    changes: list[Change] = []
    baseline_meta: dict[str, Any] | None = None
    if baseline is not None:
        old = (baseline.data or {}).get("observations") or {}
        changes = diff_observed(old, _baseline_observations(run))
        baseline_meta = {
            "id": baseline.id,
            "label": baseline.label,
            "run_id": baseline.run_id,
            "created_at": _iso(baseline.created_at),
        }

    report = build_report(
        run=_run_to_dict(run),
        results=results,
        changes=changes,
        baseline=baseline_meta,
    )
    if format == "json":
        return report
    body = render_report(report, format)
    extension = "md" if format == "markdown" else "html"
    return Response(
        content=body,
        media_type=media_type(format),
        headers={"Content-Disposition": f'inline; filename="sherlock-report-{run.id}.{extension}"'},
    )
