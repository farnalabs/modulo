"""Add otel_config_json column to organisations for per-org OTel settings.

Revision ID: 0008_otel_config
Revises: 0007_cron_trigger_columns
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_otel_config"
down_revision: str | None = "0007_cron_trigger_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column(
            "otel_config_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("organisations", "otel_config_json")
