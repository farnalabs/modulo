"""Create publishers table for verified publisher program.

Revision ID: 0032_publishers
Revises: 0031_sso_group_mappings
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032_publishers"
down_revision: str | Sequence[str] | None = "0031_sso_group_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publishers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organisation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("public_key_hex", sa.String(128), nullable=False, index=True),
        sa.Column("trust_tier", sa.String(10), nullable=False, server_default="amber"),
        sa.Column("verified_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("website_url", sa.String(2000), nullable=True),
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
        sa.UniqueConstraint("organisation_id", "public_key_hex", name="uq_publishers_org_key"),
        sa.UniqueConstraint("organisation_id", "name", name="uq_publishers_org_name"),
    )


def downgrade() -> None:
    op.drop_table("publishers")
