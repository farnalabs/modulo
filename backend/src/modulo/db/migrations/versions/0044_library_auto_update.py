"""Add auto_update column to library_primitives.

Revision ID: 0044_library_auto_update
Revises: 0043_prompt_always_visible
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0044_library_auto_update"
down_revision: str | Sequence[str] | None = "0043_prompt_always_visible"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE library_primitives ADD COLUMN IF NOT EXISTS auto_update BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE library_primitives DROP COLUMN IF EXISTS auto_update")
