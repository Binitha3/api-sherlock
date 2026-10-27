"""Phase 1 API routes: health, projects, spec import/listing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError, SpecInvalidError
from app.db.models import Project, Spec
from app.db.session import get_db
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
