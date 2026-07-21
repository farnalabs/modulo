"""Add missing performance indexes

Adds standalone indexes on columns queried by admin list endpoints and
cost/anomaly aggregation queries. Several tables lacked indexes on
commonly-filtered columns, causing full table scans and 6-20s query times.

Revision ID: 0020_add_missing_performance_indexes
Revises: 0019_pipeline_rate_limits
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "0020_add_missing_performance_indexes"
down_revision = "0019_pipeline_rate_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # remy_context_sources.organisation_id has no standalone index
    # (only in a UNIQUE constraint with user_id+source_key).
    # The /admin/remy/context-sources endpoint filters by organisation_id.
    op.create_index(
        op.f("ix_remy_context_sources_organisation_id"),
        "remy_context_sources",
        ["organisation_id"],
    )

    # org_daily_run_counts.run_date — needed for ORDER BY and range queries
    # in the anomaly detection endpoint and cost reports.
    op.create_index(
        op.f("ix_org_daily_run_counts_run_date"),
        "org_daily_run_counts",
        ["run_date"],
    )

    # Composite index for the anomaly detection query which filters by both
    # organisation_id and run_date. The existing UNIQUE constraint
    # (organisation_id, team_id, run_date) has team_id in the middle
    # so queries filtering by (org_id, run_date) cannot use it efficiently.
    op.create_index(
        op.f("ix_org_daily_run_counts_org_date"),
        "org_daily_run_counts",
        ["organisation_id", "run_date"],
    )

    # Composite index for /admin/users which filters by organisation_id
    # then joins on account_id via org_memberships. The existing UNIQUE
    # constraint has (account_id, organisation_id) — with account_id first
    # — so organisation_id-first queries don't benefit from it.
    op.create_index(
        op.f("ix_org_memberships_org_account"),
        "org_memberships",
        ["organisation_id", "account_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_org_memberships_org_account"), table_name="org_memberships")
    op.drop_index(op.f("ix_org_daily_run_counts_org_date"), table_name="org_daily_run_counts")
    op.drop_index(op.f("ix_org_daily_run_counts_run_date"), table_name="org_daily_run_counts")
    op.drop_index(op.f("ix_remy_context_sources_organisation_id"), table_name="remy_context_sources")
