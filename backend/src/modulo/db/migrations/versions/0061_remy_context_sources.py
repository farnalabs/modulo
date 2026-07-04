"""Create remy_context_sources table and add source_mode to remy_skills.

Revision ID: 0061_remy_context_sources
Revises: 0060_fix_rls_team_isolation_column
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_remy_context_sources"
down_revision: str | Sequence[str] | None = "0060_fix_rls_team_isolation_column"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remy_context_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column(
            "source_mode",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'always_on'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_index(
        "ix_remy_context_sources_lookup",
        "remy_context_sources",
        ["organisation_id", "user_id", "source_key"],
    )

    op.create_check_constraint(
        "ck_remy_context_sources_owner",
        "remy_context_sources",
        sa.text(
            "(organisation_id IS NOT NULL AND user_id IS NULL) "
            "OR (organisation_id IS NULL AND user_id IS NOT NULL)"
        ),
    )

    op.create_check_constraint(
        "ck_remy_context_sources_mode",
        "remy_context_sources",
        sa.text("source_mode IN ('always_on', 'tool', 'off')"),
    )

    op.add_column(
        "remy_skills",
        sa.Column("source_mode", sa.String(16), nullable=True),
    )

    op.execute("UPDATE remy_skills SET source_mode = 'always_on' WHERE source_mode IS NULL")


def downgrade() -> None:
    op.drop_column("remy_skills", "source_mode")
    op.drop_constraint("ck_remy_context_sources_mode", "remy_context_sources", type_="check")
    op.drop_constraint("ck_remy_context_sources_owner", "remy_context_sources", type_="check")
    op.drop_index("ix_remy_context_sources_lookup", table_name="remy_context_sources")
    op.drop_table("remy_context_sources")
