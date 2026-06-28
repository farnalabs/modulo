"""Add max_duration_seconds, max_steps, token_budget to pipelines.

Revision ID: 0040_runaway_run_protection
Revises: 0039_sync_remaining_columns
Create Date: 2026-06-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040_runaway_run_protection"
down_revision: str | Sequence[str] | None = "0039_sync_remaining_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS max_duration_seconds INTEGER")
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS max_steps INTEGER")
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS token_budget INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS max_duration_seconds")
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS max_steps")
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS token_budget")
