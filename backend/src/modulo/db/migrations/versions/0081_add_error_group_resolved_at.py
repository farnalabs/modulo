"""Add resolved_at column to error_groups table.

Revision ID: 0081_add_error_group_resolved_at
Revises: 0080_seed_tier_catalog
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081_add_error_group_resolved_at"
down_revision: str | Sequence[str] | None = "0080_seed_tier_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("error_groups", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("error_groups", "resolved_at")
