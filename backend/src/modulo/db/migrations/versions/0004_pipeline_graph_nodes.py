"""Add editable pipeline graph node storage.

Revision ID: 0004_pipeline_graph_nodes
Revises: 0003_hitl_decision_columns
Create Date: 2026-06-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_pipeline_graph_nodes"
down_revision: str | None = "0003_hitl_decision_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipelines",
        sa.Column(
            "graph_nodes_json",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("pipelines", "graph_nodes_json")
