"""Add expires_at column to org_api_keys.

Revision ID: 0018_api_key_expires_at
Revises: 0017_user_preferences
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_api_key_expires_at"
down_revision: str | Sequence[str] | None = "0017_user_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "org_api_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("org_api_keys", "expires_at")
