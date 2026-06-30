"""Add prompt_always_visible column to agents table.

Revision ID: 0043_prompt_always_visible
Revises: 0042_hitl_delivered_at
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043_prompt_always_visible"
down_revision: str | Sequence[str] | None = "0042_hitl_delivered_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS prompt_always_visible BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS prompt_always_visible")
