"""Add system bool column to schemas table

Revision ID: 0021_add_system_bool_to_schemas
Revises: 0020_add_missing_performance_indexes
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_add_system_bool_to_schemas"
down_revision = "0020_add_missing_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schemas",
        sa.Column("system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("schemas", "system")
