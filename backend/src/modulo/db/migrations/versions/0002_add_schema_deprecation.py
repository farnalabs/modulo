"""Add deprecated and deprecated_at columns to schemas table.

Revision ID: 0002_add_schema_deprecation
Revises: 0001_initial_schema
Create Date: 2026-06-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_schema_deprecation"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "schemas",
        sa.Column("deprecated", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "schemas",
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schemas", "deprecated_at")
    op.drop_column("schemas", "deprecated")
