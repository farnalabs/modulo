"""Add archived_at column to pipelines

Revision ID: 0084_add_pipeline_archived_at
Revises: 0083_add_team_settings
Create Date: 2026-07-09 12:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084_add_pipeline_archived_at"
down_revision: str | Sequence[str] | None = "0083_add_team_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pipelines", "archived_at")
