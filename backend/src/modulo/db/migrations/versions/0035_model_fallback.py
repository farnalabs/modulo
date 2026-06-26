"""Add fallback_backend_ids column to model_backends for auto-failover.

Revision ID: 0035_model_fallback
Revises: 0034_run_node_token_usage
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_model_fallback"
down_revision: str | Sequence[str] | None = "0034_run_node_token_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_backends", sa.Column("fallback_backend_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_backends", "fallback_backend_ids")
