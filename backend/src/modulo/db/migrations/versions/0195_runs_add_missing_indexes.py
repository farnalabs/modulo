"""Add batch_id and completed_at composite indexes for runs.

Revision ID: 0195_runs_add_missing_indexes
Revises: 0194_runs_index_and_constraint_fixes
Create Date: 2026-09-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0195_runs_add_missing_indexes"
down_revision = "0194_runs_index_and_constraint_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Standalone batch_id lookup index — get_batch_runs queries
    #    WHERE organisation_id = ? AND batch_id = ?. The existing ix_runs_variant_group_batch
    #    leads with variant_group_id and cannot serve batch_id-first predicates.
    op.execute("CREATE INDEX ix_runs_org_batch ON runs (organisation_id, batch_id) WHERE batch_id IS NOT NULL")

    # 2. Composite index for the outputs-sweep ORDER BY — run_node_outputs filters
    #    (organisation_id, status IN (terminal_statuses)) then ORDER BY completed_at ASC LIMIT cap.
    #    Without this index, the planner must materialize and sort the result set.
    op.execute("CREATE INDEX ix_runs_org_status_completed ON runs (organisation_id, status, completed_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_org_status_completed")
    op.execute("DROP INDEX IF EXISTS ix_runs_org_batch")
