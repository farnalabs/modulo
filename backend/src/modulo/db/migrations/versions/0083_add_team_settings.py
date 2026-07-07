"""Add settings JSON column to teams table for feature flag overrides.

Revision ID: 0083_add_team_settings
Revises: 0082_feedback_dismissed_status
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083_add_team_settings"
down_revision: str | Sequence[str] | None = "0082_feedback_dismissed_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("teams", "settings")
