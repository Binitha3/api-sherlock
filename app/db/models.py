"""SQLAlchemy 2.0 models.

Auth secrets (tokens, API keys, passwords) are NEVER stored here. They are
passed per run in the request body or environment variables and kept only in
memory for the duration of that run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Microsecond-precision UTC timestamp (server_default now() is second-granular)."""
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    specs: Mapped[list[Spec]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    environments: Mapped[list[Environment]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list[Run]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    baselines: Mapped[list[Baseline]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Spec(Base):
    __tablename__ = "specs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_label: Mapped[str] = mapped_column(String(120), nullable=False)
    openapi_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resolved_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    operations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    project: Mapped[Project] = relationship(back_populates="specs")


class Environment(Base):
    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    auth_header_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    project: Mapped[Project] = relationship(back_populates="environments")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    spec_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("specs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("environments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    include_operations: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    parameter_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="runs")
    results: Mapped[list[RunResult]] = relationship(  # noqa: F821
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class RunResult(Base):
    __tablename__ = "run_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(300), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # passed | failed | skipped
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    violations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    auth_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    run: Mapped[Run] = relationship(back_populates="results")


class Baseline(Base):
    __tablename__ = "baselines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="baseline")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    project: Mapped[Project] = relationship(back_populates="baselines")
