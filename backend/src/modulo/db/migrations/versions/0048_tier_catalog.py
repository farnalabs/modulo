"""Create tier_catalog and feature_flag_catalog tables.

Revision ID: 0048_tier_catalog
Revises: 0047_create_missing_tables
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_tier_catalog"
down_revision: str | Sequence[str] | None = "0047_create_missing_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tier_catalog",
        sa.Column("tier_id", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("requires_license", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("description", sa.Text()),
    )

    op.create_table(
        "feature_flag_catalog",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("description", sa.Text()),
        sa.Column("tier_id", sa.Text(), sa.ForeignKey("tier_catalog.tier_id"), nullable=False),
        sa.Column("depends_on", sa.ARRAY(sa.Text())),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_table("feature_flag_catalog")
    op.drop_table("tier_catalog")
