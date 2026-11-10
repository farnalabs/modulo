"""Add environment profile wall-clock capability (FAR-1359).

Revision ID: 0338_environment_profile_max_node_seconds
Revises: 0337_per_node_completion
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0338_environment_profile_max_node_seconds"
down_revision: str | None = "0337_per_node_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "environment_profiles",
        sa.Column("max_node_seconds", sa.Integer(), nullable=True, server_default="3300"),
    )
    op.execute(sa.text("UPDATE environment_profiles SET max_node_seconds = 3300 WHERE max_node_seconds IS NULL"))
    op.alter_column("environment_profiles", "max_node_seconds", nullable=False)


def downgrade() -> None:
    op.drop_column("environment_profiles", "max_node_seconds")
