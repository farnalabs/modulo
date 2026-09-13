"""Add workspace_inputs_count column to run_daily_facts.

Revision ID: 0227_add_workspace_inputs_count
Revises: 0226_agents_json_to_jsonb
Create Date: 2026-09-13

Adds a nullable INTEGER column ``workspace_inputs_count`` to the
``run_daily_facts`` table.  The live analytics writer
(``record_run_facts``) references this column; without the migration
the INSERT/UPDATE would fail with UndefinedColumn on every write.
"""

import sqlalchemy as sa
from alembic import op

revision = "0227_add_workspace_inputs_count"
down_revision = "0226_agents_json_to_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "run_daily_facts",
        sa.Column("workspace_inputs_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("run_daily_facts", "workspace_inputs_count")
