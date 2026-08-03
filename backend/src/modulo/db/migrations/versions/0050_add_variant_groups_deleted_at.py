"""Add deleted_at to variant_groups for soft delete

Revision ID: 0023_add_variant_groups_deleted_at
Revises: 0022_add_lifecycle_maps_deleted_at
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_add_variant_groups_deleted_at"
down_revision = "0022_add_lifecycle_maps_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("variant_groups", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("variant_groups", "deleted_at")
