"""Add deleted_at to schemas for soft delete

Revision ID: 0023_add_schemas_deleted_at
Revises: 0022_add_agents_deleted_at
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_add_schemas_deleted_at"
down_revision = "0022_add_agents_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schemas", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("schemas", "deleted_at")
