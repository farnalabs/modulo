"""Add composite indexes for dashboard queries.

Revision ID: 0053_dashboard_perf_indexes
Revises: 0052_composite_bindings
Create Date: 2026-07-01 12:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0053_dashboard_perf_indexes"
down_revision: str | Sequence[str] | None = "0052_composite_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_runs_org_status",
        "runs",
        ["organisation_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_runs_org_owner_status",
        "runs",
        ["organisation_id", "owner_team_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_runs_org_created",
        "runs",
        ["organisation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_eval_results_org_evaluated_at",
        "eval_results",
        ["organisation_id", "evaluated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runs_org_status", table_name="runs")
    op.drop_index("ix_runs_org_owner_status", table_name="runs")
    op.drop_index("ix_runs_org_created", table_name="runs")
    op.drop_index("ix_eval_results_org_evaluated_at", table_name="eval_results")
