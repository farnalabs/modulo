"""Create error_notification_rules table.

Revision ID: 0054_error_notification_rules
Revises: 0053_dashboard_perf_indexes
Create Date: 2026-07-01 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_error_notification_rules"
down_revision: str | Sequence[str] | None = "0053_dashboard_perf_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "error_notification_rules",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organisation_id", sa.Uuid(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("condition_level", sa.String(20), nullable=False, server_default=sa.text("'error'")),
        sa.Column("condition_min_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("condition_window_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),
        sa.Column("action_type", sa.String(20), nullable=False, server_default=sa.text("'in_app'")),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("condition_level IN ('error', 'warning', 'critical')", name="ck_enr_condition_level"),
        sa.CheckConstraint("action_type IN ('in_app', 'email', 'webhook')", name="ck_enr_action_type"),
    )


def downgrade() -> None:
    op.drop_table("error_notification_rules")
