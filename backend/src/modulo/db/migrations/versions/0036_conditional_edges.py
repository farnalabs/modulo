"""Add condition_expression column and 'conditional' edge_type to pipeline_edges.

Revision ID: 0036_conditional_edges
Revises: 0035_model_fallback
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_conditional_edges"
down_revision: str | Sequence[str] | None = "0035_model_fallback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_edges",
        sa.Column("condition_expression", sa.String(500), nullable=True),
    )
    op.execute("ALTER TABLE pipeline_edges DROP CONSTRAINT IF EXISTS ck_pipeline_edges_type")
    op.execute(
        "ALTER TABLE pipeline_edges ADD CONSTRAINT ck_pipeline_edges_type "
        "CHECK (edge_type IN ('normal', 'reject', 'conditional'))"
    )
    op.alter_column(
        "pipeline_edges",
        "edge_type",
        type_=sa.String(15),
        existing_type=sa.String(10),
        nullable=False,
        server_default="normal",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pipeline_edges DROP CONSTRAINT IF EXISTS ck_pipeline_edges_type")
    op.execute(
        "ALTER TABLE pipeline_edges ADD CONSTRAINT ck_pipeline_edges_type CHECK (edge_type IN ('normal', 'reject'))"
    )
    op.alter_column(
        "pipeline_edges",
        "edge_type",
        type_=sa.String(10),
        existing_type=sa.String(15),
        nullable=False,
        server_default="normal",
    )
    op.drop_column("pipeline_edges", "condition_expression")
