"""Add error_code and claimed_by indexes for runs.

Revision ID: 0202_runs_error_code_claimed_by_indexes
Revises: 0201_spend_anomaly_unique_org_date
Create Date: 2026-09-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0202_runs_error_code_claimed_by_indexes"
down_revision = "0201_spend_anomaly_unique_org_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Error-code analytics index — the analytics builder filters on
    #    RunDailyFact.error_code (a copy of runs.error_code); ad-hoc error
    #    lookups on the runs table (e.g. "find all stalled runs for this org")
    #    currently require a sequential scan.  Low cardinality (~20 distinct
    #    values) means the index selectivity is moderate, but it still avoids
    #    a full table scan on the hottest large table.
    op.execute(
        "CREATE INDEX ix_runs_org_error_code ON runs (organisation_id, error_code) "
        "WHERE error_code IS NOT NULL"
    )

    # 2. Claimed-by index — HITL claim operations set runs.claimed_by and
    #    subsequent queries filter by (organisation_id, claimed_by) to find
    #    claims held by a specific reviewer.  Without this index the lookup
    #    scans the full runs table.
    op.execute(
        "CREATE INDEX ix_runs_org_claimed_by ON runs (organisation_id, claimed_by) "
        "WHERE claimed_by IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_org_claimed_by")
    op.execute("DROP INDEX IF EXISTS ix_runs_org_error_code")
