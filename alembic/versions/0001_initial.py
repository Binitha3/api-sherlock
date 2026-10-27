"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "specs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("version_label", sa.String(length=120), nullable=False),
        sa.Column("openapi_version", sa.String(length=20), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_document", sa.JSON(), nullable=False),
        sa.Column("resolved_document", sa.JSON(), nullable=False),
        sa.Column("operations", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_specs_project_id", "specs", ["project_id"])

    op.create_table(
        "environments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.String(length=2000), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("auth_header_name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_environments_project_id", "environments", ["project_id"])

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("spec_id", sa.String(length=32), nullable=False),
        sa.Column("environment_id", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=2000), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("include_operations", sa.JSON(), nullable=True),
        sa.Column("parameter_values", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["spec_id"], ["specs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_project_id", "runs", ["project_id"])
    op.create_index("ix_runs_spec_id", "runs", ["spec_id"])
    op.create_index("ix_runs_environment_id", "runs", ["environment_id"])

    op.create_table(
        "run_results",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("operation_id", sa.String(length=300), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("violations", sa.JSON(), nullable=False),
        sa.Column("auth_hint", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("observed_schema", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_results_run_id", "run_results", ["run_id"])

    op.create_table(
        "baselines",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_baselines_project_id", "baselines", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_baselines_project_id", table_name="baselines")
    op.drop_table("baselines")
    op.drop_index("ix_run_results_run_id", table_name="run_results")
    op.drop_table("run_results")
    op.drop_index("ix_runs_environment_id", table_name="runs")
    op.drop_index("ix_runs_spec_id", table_name="runs")
    op.drop_index("ix_runs_project_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_environments_project_id", table_name="environments")
    op.drop_table("environments")
    op.drop_index("ix_specs_project_id", table_name="specs")
    op.drop_table("specs")
    op.drop_table("projects")
