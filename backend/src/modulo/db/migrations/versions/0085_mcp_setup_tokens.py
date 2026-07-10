"""Add mcp_setup_tokens table, make model_backends.credentials_ciphertext nullable.

Revision ID: 0085_mcp_setup_tokens
Revises: 0051_error_tracking, 0084_add_pipeline_archived_at
Create Date: 2026-07-10 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085_mcp_setup_tokens"
down_revision: str | Sequence[str] | None = ("0051_error_tracking", "0084_add_pipeline_archived_at")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_setup_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.alter_column("model_backends", "credentials_ciphertext", nullable=True, existing_type=sa.LargeBinary())


def downgrade() -> None:
    op.alter_column("model_backends", "credentials_ciphertext", nullable=False, existing_type=sa.LargeBinary())
    op.drop_table("mcp_setup_tokens")
