"""Add deleted_at to environment_profiles for soft delete

Revision ID: 0022_add_environment_profiles_deleted_at
Revises: 0021_add_pipelines_deleted_at
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_add_environment_profiles_deleted_at"
down_revision = "0021_add_pipelines_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("environment_profiles", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("environment_profiles", "deleted_at")
