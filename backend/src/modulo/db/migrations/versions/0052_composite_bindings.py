"""Add composite_bindings_json column to pipeline_snapshots.

Revision ID: 0052_composite_bindings
Revises: 0051_composite_library_type
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_composite_bindings"
down_revision: str | Sequence[str] | None = "0051_composite_library_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_snapshots",
        sa.Column(
            "composite_bindings_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pipeline_snapshots", "composite_bindings_json")
