"""Create sso_providers table.

Revision ID: 0021_sso_providers
Revises: 0020_delivery_log_response_body
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_sso_providers"
down_revision: str | Sequence[str] | None = "0020_delivery_log_response_body"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sso_providers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider_type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("client_id", sa.String(1024), nullable=True),
        sa.Column("client_secret", sa.String(2048), nullable=True),
        sa.Column("discovery_url", sa.String(2048), nullable=True),
        sa.Column("metadata_url", sa.String(2048), nullable=True),
        sa.Column("metadata_xml", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.String(1024), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("auto_provision", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("default_role", sa.String(32), nullable=False, server_default="runner"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sso_providers")
