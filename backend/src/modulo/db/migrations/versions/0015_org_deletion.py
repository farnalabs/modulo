"""Add deletion_token, deletion_token_expires_at, export_bundle_json to organisations.

Revision ID: 0015_org_deletion
Revises: 0014_team_cost_attribution, 0014_fixture_contribution
Create Date: 2026-06-24 08:08:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_org_deletion"
down_revision: str | Sequence[str] | None = (
    "0014_team_cost_attribution",
    "0014_fixture_contribution",
)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column("deletion_token", sa.String(128), nullable=True),
    )
    op.add_column(
        "organisations",
        sa.Column(
            "deletion_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "organisations",
        sa.Column("export_bundle_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organisations", "deletion_token")
    op.drop_column("organisations", "deletion_token_expires_at")
    op.drop_column("organisations", "export_bundle_json")
