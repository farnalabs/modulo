"""Add token-family refresh-reuse grace tracking column.

Revision ID: 0230_token_families_refresh_grace
Revises: 0229_add_workspace_inputs_count
Create Date: 2026-09-14

Adds a nullable, timezone-aware ``rotated_at`` column to ``token_families``
so refresh-token reuse can be tolerated within a grace window (FAR-819): a
reuse arriving within ``REFRESH_REUSE_GRACE_SECONDS`` of the last rotation
is treated as a benign reuse-interval replay (advance + mint), while reuse
outside the window blacklists the family as theft. The family advances
normally on every reuse within the window — no per-window budget, no steps
tolerance, no 409 path.
"""

import sqlalchemy as sa
from alembic import op

revision = "0230_token_families_refresh_grace"
down_revision = "0229_add_workspace_inputs_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "token_families",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("token_families", "rotated_at")
