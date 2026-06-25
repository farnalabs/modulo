"""Add last_login to users and create token_families table.

Revision ID: 0010_user_last_login_token_families
Revises: 0009_polling_trigger_events_check
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_user_last_login_token_families"
down_revision: str | None = "0009_polling_trigger_events_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "token_families",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("max_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_blacklisted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("blacklisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("family_id"),
    )
    op.create_index(op.f("ix_token_families_user_id"), "token_families", ["user_id"], unique=False)
    op.create_index(op.f("ix_token_families_organisation_id"), "token_families", ["organisation_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_token_families_organisation_id"), table_name="token_families")
    op.drop_index(op.f("ix_token_families_user_id"), table_name="token_families")
    op.drop_table("token_families")
    op.drop_column("users", "last_login")
