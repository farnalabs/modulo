"""Add SAQ routing columns to runs + 'saq' error source (PR B).

Revision ID: 0030_add_saq_routing_columns
Revises: 0029_fix_stale_run_timeout_non_null
Create Date: 2026-07-31

Adds the three dispatch-tracking columns (dispatcher, saq_job_id,
claim_token) to the runs table and extends the error_events source CHECK
constraint to accept 'saq' (the SAQ error source introduced by the
Celery->SAQ migration).

Down-revisions for the three columns are written (column-level rollback).
The 'saq' enum value is permanent (PostgreSQL cannot drop an enum value
still referenced by rows) — the constraint downgrade is included for
symmetric migration hygiene, matching the existing constraint.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_add_saq_routing_columns"
down_revision = "0029_fix_stale_run_timeout_non_null"


def upgrade() -> None:
    op.add_column("runs", sa.Column("dispatcher", sa.String(20), nullable=True))
    op.create_index("ix_runs_dispatcher", "runs", ["dispatcher"])
    op.add_column("runs", sa.Column("saq_job_id", sa.String(255), nullable=True))
    op.add_column("runs", sa.Column("claim_token", sa.String(128), nullable=True))

    op.drop_constraint("ck_error_events_source", "error_events", type_="check")
    op.create_check_constraint(
        "ck_error_events_source",
        "error_events",
        "source IN ('backend', 'frontend', 'celery', 'saq')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_error_events_source", "error_events", type_="check")
    op.create_check_constraint(
        "ck_error_events_source",
        "error_events",
        "source IN ('backend', 'frontend', 'celery')",
    )

    op.drop_index("ix_runs_dispatcher", table_name="runs")
    op.drop_column("runs", "claim_token")
    op.drop_column("runs", "saq_job_id")
    op.drop_column("runs", "dispatcher")
