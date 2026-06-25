"""Add response_body column to notification_delivery_log.

Revision ID: 0020_delivery_log_response_body
Revises: 0019_pipeline_templates
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_delivery_log_response_body"
down_revision: str | None = "0019_pipeline_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_delivery_log",
        sa.Column("response_body", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_delivery_log", "response_body")
