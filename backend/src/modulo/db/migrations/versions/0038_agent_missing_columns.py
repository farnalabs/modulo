"""Add max_input_length and library_id to agents (token_budget already exists).

Revision ID: 0038_agent_missing_columns
Revises: 0036_conditional_edges
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_agent_missing_columns"
down_revision: str | Sequence[str] | None = "0036_conditional_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_input_length INTEGER")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS token_budget INTEGER")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS library_id UUID")
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_agents_library_id') THEN "
        "ALTER TABLE agents ADD CONSTRAINT fk_agents_library_id "
        "FOREIGN KEY (library_id) REFERENCES library_primitives(id) ON DELETE SET NULL; "
        "END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS fk_agents_library_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS library_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS token_budget")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS max_input_length")
