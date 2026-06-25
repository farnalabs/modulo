"""Add previous_hash to audit_events and create audit_chain_heads table.

Revision ID: 0008_audit_chaining
Revises: 0007_cron_trigger_columns
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_audit_chaining"
down_revision: str | None = "0007_cron_trigger_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("previous_hash", sa.Text(), nullable=True),
    )

    op.create_table(
        "audit_chain_heads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("last_event_hash", sa.Text(), nullable=False),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_event_id"], ["audit_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id"),
    )
    op.create_index(
        "ix_audit_chain_heads_org",
        "audit_chain_heads",
        ["organisation_id"],
    )


def downgrade() -> None:
    op.drop_table("audit_chain_heads")
    op.drop_column("audit_events", "previous_hash")
