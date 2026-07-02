"""Create notifications and dismissals tables.

Revision ID: 0057_notifications
Revises: 0056_modulo_source
Create Date: 2026-07-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_notifications"
down_revision: str | Sequence[str] | None = "0056_modulo_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("action_url", sa.String(2048), nullable=True),
        sa.Column("dismiss_strategy", sa.String(20), nullable=False, server_default="user_only"),
        sa.Column("dismissible_at_scope", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("scope IN ('user', 'org', 'admin')", name="ck_notifications_scope"),
        sa.CheckConstraint("level IN ('debug', 'info', 'warning', 'error')", name="ck_notifications_level"),
        sa.CheckConstraint("dismiss_strategy IN ('user_only', 'org_admin', 'any_scope')", name="ck_notifications_dismiss_strategy"),
    )

    op.create_table(
        "dismissals",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("notification_id", sa.Uuid(), sa.ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("dismissed_by_user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dismiss_scope", sa.String(20), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("dismiss_scope IN ('self', 'scope')", name="ck_dismissals_scope"),
        sa.UniqueConstraint("notification_id", "dismissed_by_user_id", name="uq_dismissal_user_notification"),
    )


def downgrade() -> None:
    op.drop_table("dismissals")
    op.drop_table("notifications")
