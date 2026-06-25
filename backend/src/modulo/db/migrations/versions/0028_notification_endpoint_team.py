"""Add team_id column to notification_endpoints for team-scoped webhooks.

Revision ID: 0028_notification_endpoint_team
Revises: 0027_hitl_claim_team
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_notification_endpoint_team"
down_revision: str | Sequence[str] | None = "0027_hitl_claim_team"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_endpoints",
        sa.Column(
            "team_id",
            sa.Uuid(),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_notification_endpoints_team_id"),
        "notification_endpoints",
        ["team_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_notification_endpoints_team_id"),
        table_name="notification_endpoints",
    )
    op.drop_column("notification_endpoints", "team_id")
