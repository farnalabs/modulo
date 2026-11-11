"""FAR-900: add schema_profile to agents.

Adds ``schema_profile`` (String(30), nullable) to ``agents``.  The column
carries the Agent-level default schema translation profile; nodes override it
via their own ``schema_profile`` field in ``graph_nodes_json``.

Schema legs (Postgres only; SQLite/ORM-created test schemas get the column
from the ``Agent`` model's ``create_all``):

1. ``agents.schema_profile`` — String(30), nullable.  NULL = verbatim
   (identity, the default when no profile is selected).

No data legs: existing rows get NULL (verbatim default).

Downgrade: drops the column and its CHECK constraint.

Revision ID: 0248_agent_schema_profile
Revises: 0247_sso_presets
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0248_agent_schema_profile"
down_revision = "0247_sso_presets"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("agents")}
    if "schema_profile" in columns:
        return

    op.add_column("agents", sa.Column("schema_profile", sa.String(30), nullable=True))
    op.create_check_constraint(
        "ck_agents_schema_profile",
        "agents",
        "schema_profile IS NULL OR schema_profile IN ('verbatim', 'provider-strict', 'runtime-sdk')",
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("agents")}
    if "schema_profile" not in columns:
        return

    op.drop_constraint("ck_agents_schema_profile", "agents", type_="check")
    op.drop_column("agents", "schema_profile")
