"""Add delivered_at column to hitl_claims.

Revision ID: 0042_hitl_delivered_at
Revises: 0041_agent_is_executable
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042_hitl_delivered_at"
down_revision: str | Sequence[str] | None = "0041_agent_is_executable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE hitl_claims ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE hitl_claims DROP COLUMN IF EXISTS delivered_at")
