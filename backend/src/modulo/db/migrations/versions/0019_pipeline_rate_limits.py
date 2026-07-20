"""Add pipeline rate_limit_config and run.rate_limit_key

Revision ID: 0019_pipeline_rate_limits
Revises: 0018_web_vital_events
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_pipeline_rate_limits"
down_revision = "0018_web_vital_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("rate_limit_config", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("rate_limit_key", sa.String(512), nullable=True))
    op.create_index(op.f("ix_runs_rate_limit_key"), "runs", ["rate_limit_key"])


def downgrade() -> None:
    op.drop_index(op.f("ix_runs_rate_limit_key"), table_name="runs")
    op.drop_column("runs", "rate_limit_key")
    op.drop_column("pipelines", "rate_limit_config")
