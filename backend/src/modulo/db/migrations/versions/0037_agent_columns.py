"""Add max_input_length, token_budget, library_id to agents.

Revision ID: 0037_agent_columns
Revises: 0036_conditional_edges
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_agent_columns"
down_revision: str | Sequence[str] | None = "0036_conditional_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("max_input_length", sa.Integer(), nullable=True))
    op.add_column("agents", sa.Column("token_budget", sa.Integer(), nullable=True))
    op.add_column("agents", sa.Column("library_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_agents_library_id",
        "agents",
        "library_primitives",
        ["library_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agents_library_id", "agents", type_="foreignkey")
    op.drop_column("agents", "library_id")
    op.drop_column("agents", "token_budget")
    op.drop_column("agents", "max_input_length")
