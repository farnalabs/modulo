"""Create system_config table for deployment-wide DB-backed settings.

Revision ID: 0046_system_config
Revises: 0045_saved_views
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_system_config"
down_revision: str | Sequence[str] | None = "0045_saved_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0045_account_org_membership"


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_system_config_key"),
    )


def downgrade() -> None:
    op.drop_table("system_config")
