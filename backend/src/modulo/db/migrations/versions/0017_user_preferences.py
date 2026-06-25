"""Add preferences JSON column to users table.

Revision ID: 0017_user_preferences
Revises: 0016_oauth_tables, 0012_team_memberships, 0012_feedback_records, 0008_audit_chaining, 0007_pipeline_snapshot_tag_notes, 0006_primitive_ratings
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_user_preferences"
down_revision: str | Sequence[str] | None = (
    "0016_oauth_tables",
    "0012_team_memberships",
    "0012_feedback_records",
    "0008_audit_chaining",
    "0007_pipeline_snapshot_tag_notes",
    "0006_primitive_ratings",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferences", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )


def downgrade() -> None:
    op.drop_column("users", "preferences")
