"""Add deleted_at to pipelines for soft delete

Revision ID: 0021_add_pipelines_deleted_at
Revises: 0020_add_missing_performance_indexes
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_add_pipelines_deleted_at"
down_revision = "0020_add_missing_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pipelines", "deleted_at")
