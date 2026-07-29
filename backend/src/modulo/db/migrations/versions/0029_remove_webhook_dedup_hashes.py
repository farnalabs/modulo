"""Drop webhook_dedup_hashes table — moved to Redis with auto-expiry.

Revision ID: 0029_remove_webhook_dedup_hashes
Revises: 0028_add_claimed_by_to_runs
Create Date: 2026-07-29 12:00:00.000000
"""

from __future__ import annotations

from typing import ClassVar

import sqlalchemy as sa
from alembic import op

revision: str = "0029_remove_webhook_dedup_hashes"
down_revision: str | None = "0028_add_claimed_by_to_runs"
branch_labels: ClassVar[set[str] | None] = None
depends_on: ClassVar[set[str] | None] = None


def upgrade() -> None:
    op.drop_table("webhook_dedup_hashes")


def downgrade() -> None:
    op.create_table(
        "webhook_dedup_hashes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trigger_id", "payload_hash", name="uq_webhook_dedup_trigger_hash"),
    )
    op.create_index("ix_webhook_dedup_hashes_expires_at", "webhook_dedup_hashes", ["expires_at"])
    op.create_index("ix_webhook_dedup_hashes_organisation_id", "webhook_dedup_hashes", ["organisation_id"])
    op.create_index("ix_webhook_dedup_hashes_trigger_id", "webhook_dedup_hashes", ["trigger_id"])
