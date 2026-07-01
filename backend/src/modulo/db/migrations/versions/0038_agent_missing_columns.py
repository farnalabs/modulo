"""Add max_input_length, token_budget, library_id to agents.

This migration was partially applied. It exists as a stub so alembic can
walk the chain from 0036 → 0038 → 0039.

Revision ID: 0038_agent_missing_columns
Revises: 0036_conditional_edges
Create Date: 2026-06-28
"""

from collections.abc import Sequence

revision: str = "0038_agent_missing_columns"
down_revision: str | Sequence[str] | None = "0036_conditional_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
