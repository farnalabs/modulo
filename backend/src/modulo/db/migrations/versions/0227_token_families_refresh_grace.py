"""Add token-family refresh-reuse grace tracking columns.

Revision ID: 0227_token_families_refresh_grace
Revises: 0226_agents_json_to_jsonb
Create Date: 2026-09-14

Adds nullable, timezone-aware ``rotated_at`` and ``reuse_window_started_at``
columns to ``token_families`` so refresh-token reuse can be tolerated within a
grace window (FAR-819) without losing the theft signal entirely: a reuse
arriving within ``REFRESH_REUSE_GRACE_SECONDS`` of the last rotation is
accepted and the family's reuse window start is recorded, while reuse beyond
the window (or beyond the per-window budget) still blacklists the family.
"""

import sqlalchemy as sa
from alembic import op

revision = "0227_token_families_refresh_grace"
down_revision = "0226_agents_json_to_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "token_families",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "token_families",
        sa.Column("reuse_window_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("token_families", "reuse_window_started_at")
    op.drop_column("token_families", "rotated_at")
