"""Add deleted_at to accounts for soft delete

Revision ID: 0023_add_accounts_deleted_at
Revises: 0022_add_teams_deleted_at
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_add_accounts_deleted_at"
down_revision = "0022_add_teams_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "deleted_at")
