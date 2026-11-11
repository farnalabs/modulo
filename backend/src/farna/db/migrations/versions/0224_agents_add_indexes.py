"""Add performance indexes on agents and drop duplicate account_id index.

Revision ID: 0224_agents_add_indexes
Revises: 0223_agents_add_foreign_keys
Create Date: 2026-09-13

Indexes added:
  - ``(organisation_id, created_at DESC)`` — covers the paginated list
    query ``WHERE organisation_id = … ORDER BY created_at DESC``.
  - ``(input_schema_id)`` — speeds up schema-reference lookups in
    ``crud/schema.py`` and ``housekeeping.py``.
  - ``(output_schema_id)`` — same rationale.

Duplicate index ``ix_agent_account_id`` (singular, created by 0110) is
dropped; ``ix_agents_account_id`` (plural, created by 0142) is the
canonical replacement.
"""

import sqlalchemy as sa
from alembic import op

revision = "0224_agents_add_indexes"
down_revision = "0223_agents_add_foreign_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_agents_organisation_id_created_at",
        "agents",
        ["organisation_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_agents_input_schema_id",
        "agents",
        ["input_schema_id"],
        unique=False,
    )
    op.create_index(
        "ix_agents_output_schema_id",
        "agents",
        ["output_schema_id"],
        unique=False,
    )
    op.execute("DROP INDEX IF EXISTS ix_agent_account_id;")


def downgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_account_id ON public.agents USING btree (account_id);")
    op.drop_index("ix_agents_output_schema_id", table_name="agents")
    op.drop_index("ix_agents_input_schema_id", table_name="agents")
    op.drop_index("ix_agents_organisation_id_created_at", table_name="agents")
