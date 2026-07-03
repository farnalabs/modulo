"""Add annotation column to feedback_records.

Revision ID: 0059_feedback_annotation
Revises: 0058_uuid_friendly_ids
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_feedback_annotation"
down_revision: str | None = "0058_uuid_friendly_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feedback_records",
        sa.Column("annotation", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("feedback_records", "annotation")
