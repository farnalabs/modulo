"""Make agents schema/backend references nullable (FAR-31).

Revision ID: 0089_agents_nullable_schema_fields
Revises: 0088_drop_stages
Create Date: 2026-08-12

The MCP ``create_agent`` tool previously sentineled omitted schema IDs as
``uuid.UUID(int=0)``, which failed the composite FK to ``schema_versions``
(no row with that id exists). An agent may now be created without
input/output schema bindings or a model backend — NULL means "not yet
bound", matching the REST schemas flow where a schema and its ``latest``
version are created before the agent.

The composite FKs (``fk_agents_input_schema_version`` /
``fk_agents_output_schema_version``) use Postgres MATCH SIMPLE semantics: a
NULL schema id relaxes the whole constraint, while agents that DO bind a
schema are still enforced exactly as before. The single-column
``model_backends`` FK is likewise relaxed by NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089_agents_nullable_schema_fields"
down_revision: str | None = "0088_drop_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("agents", "input_schema_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("agents", "input_schema_version", existing_type=sa.String(length=50), nullable=True)
    op.alter_column("agents", "output_schema_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("agents", "output_schema_version", existing_type=sa.String(length=50), nullable=True)
    op.alter_column("agents", "model_backend_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.alter_column("agents", "model_backend_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("agents", "output_schema_version", existing_type=sa.String(length=50), nullable=False)
    op.alter_column("agents", "output_schema_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("agents", "input_schema_version", existing_type=sa.String(length=50), nullable=False)
    op.alter_column("agents", "input_schema_id", existing_type=sa.Uuid(), nullable=False)
