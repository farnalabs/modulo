"""Create chat_sessions, chat_messages, and remy_skills tables.

Revision ID: 0049_remy_tables
Revises: 0048_tier_catalog
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_remy_tables"
down_revision: str | Sequence[str] | None = "0048_tier_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("system_prompt_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("tool_calls_json", sa.JSON()),
        sa.Column("tool_results_json", sa.JSON()),
        sa.Column("token_count", sa.Integer()),
        sa.Column("parent_id", sa.Uuid(), sa.ForeignKey("chat_messages.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_table(
        "remy_skills",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("triggers", sa.JSON()),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_remy_skills_organisation_id", "remy_skills", ["organisation_id"])
    op.create_index("ix_remy_skills_user_id", "remy_skills", ["user_id"])

    op.create_check_constraint(
        "ck_remy_skills_owner",
        "remy_skills",
        sa.text(
            "(organisation_id IS NOT NULL AND user_id IS NULL) "
            "OR (organisation_id IS NULL AND user_id IS NOT NULL)"
        ),
    )

    op.create_check_constraint(
        "ck_chat_messages_role",
        "chat_messages",
        sa.text("role IN ('user', 'assistant', 'tool_use', 'tool_result', 'summary')"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_messages_role", "chat_messages", type_="check")
    op.drop_constraint("ck_remy_skills_owner", "remy_skills", type_="check")
    op.drop_table("remy_skills")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
