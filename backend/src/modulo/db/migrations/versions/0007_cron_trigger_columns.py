"""Add cron_expression, cron_timezone, last_fired_at, next_fire_at to triggers.

Revision ID: 0007_cron_trigger_columns
Revises: 0006_notification_endpoints
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_cron_trigger_columns"
down_revision: str | None = "0006_notification_endpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "triggers",
        sa.Column("cron_expression", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "triggers",
        sa.Column("cron_timezone", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "triggers",
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "triggers",
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_triggers_next_fire_at"), "triggers", ["next_fire_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_triggers_next_fire_at"), table_name="triggers")
    op.drop_column("triggers", "next_fire_at")
    op.drop_column("triggers", "last_fired_at")
    op.drop_column("triggers", "cron_timezone")
    op.drop_column("triggers", "cron_expression")
