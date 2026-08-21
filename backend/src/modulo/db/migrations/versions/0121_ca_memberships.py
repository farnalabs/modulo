"""add customer_accounts and user_memberships tables

Revision ID: 0121_ca_memberships
Revises: 0120_org_fk_hardening
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0121_ca_memberships"
down_revision = "0120_org_fk_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_accounts",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("settings_json", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("status IN ('active', 'suspended', 'deleted')", name="ck_customer_accounts_status"),
    )
    op.create_table(
        "user_memberships",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", UUID(), sa.ForeignKey("customer_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(50), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("account_id", "user_id", name="uq_user_memberships_account_user"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_user_memberships_role"),
    )
    op.create_index("ix_customer_accounts_slug", "customer_accounts", ["slug"])
    op.create_index("ix_user_memberships_account_id", "user_memberships", ["account_id"])
    op.create_index("ix_user_memberships_user_id", "user_memberships", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_memberships")
    op.drop_table("customer_accounts")
