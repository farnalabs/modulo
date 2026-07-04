"""Add tag and notes columns to pipeline_snapshots for versioning.

Revision ID: 0067_pipeline_snapshot_tag_notes
Revises: 0006_notification_endpoints
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_pipeline_snapshot_tag_notes"
down_revision: str | None = "0006_notification_endpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_snapshots",
        sa.Column("tag", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "pipeline_snapshots",
        sa.Column("notes", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_snapshots", "notes")
    op.drop_column("pipeline_snapshots", "tag")
