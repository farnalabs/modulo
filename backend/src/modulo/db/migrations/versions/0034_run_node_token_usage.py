"""Add node_token_usage JSON column to runs table.

Revision ID: 0034_run_node_token_usage
Revises: 0033_correction_run
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_run_node_token_usage"
down_revision: str | Sequence[str] | None = "0033_correction_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("node_token_usage", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "node_token_usage")
