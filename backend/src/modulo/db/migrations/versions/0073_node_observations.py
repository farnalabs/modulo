"""Add node_observations table for tracking human observation of node output.

Revision ID: 0073_node_observations
Revises: 0040_runaway_run_protection
Create Date: 2026-06-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0073_node_observations"
down_revision: str | Sequence[str] | None = "0040_runaway_run_protection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS node_observations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
            run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            node_id VARCHAR(255) NOT NULL,
            human_observed_by UUID REFERENCES users(id) ON DELETE SET NULL,
            human_observed_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, node_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_node_observations_run_id ON node_observations(run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_node_observations_organisation_id ON node_observations(organisation_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS node_observations")
