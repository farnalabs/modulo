"""Add group_mappings column to sso_providers.

Revision ID: 0031_sso_group_mappings
Revises: 0030_contribution_versions
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_sso_group_mappings"
down_revision: str | Sequence[str] | None = "0030_contribution_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sso_providers",
        sa.Column(
            "group_mappings",
            postgresql.JSON,
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sso_providers", "group_mappings")
