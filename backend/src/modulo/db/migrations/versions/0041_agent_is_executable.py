"""Add is_executable column to agents table.

Revision ID: 0041_agent_is_executable
Revises: 0040_runaway_run_protection
Create Date: 2026-06-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041_agent_is_executable"
down_revision: str | Sequence[str] | None = "0040_runaway_run_protection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_executable BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS is_executable")
