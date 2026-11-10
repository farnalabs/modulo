"""Add error_code index for runs.

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
    # Error-code analytics index — error_tracking.py:345-349 filters on
    # (organisation_id, error_code IN capacity markers) on the runs table, and
    # the RLS-scoped failure-reason breakdown (crud/run.py:3168-3180) filters
    # error_code IS NOT NULL and groups by it.  Without this index those
    # lookups scan the full runs table (the hottest large table).
    op.execute("CREATE INDEX ix_runs_org_error_code ON runs (organisation_id, error_code) WHERE error_code IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_org_error_code")
