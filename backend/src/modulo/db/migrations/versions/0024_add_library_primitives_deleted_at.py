"""Add deleted_at to library_primitives for soft delete

Revision ID: 0024_add_library_primitives_deleted_at
Revises: 0023_add_parameter_sets_deleted_at
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_add_library_primitives_deleted_at"
down_revision = "0023_add_parameter_sets_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("library_primitives", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("library_primitives", "deleted_at")
