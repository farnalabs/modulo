"""Add error_detail column to runs table.

Revision ID: 0023_run_error_detail
Revises: 0022_cost_export_anomalies
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_run_error_detail"
down_revision: str | Sequence[str] | None = "0022_cost_export_anomalies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("error_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "error_detail")
