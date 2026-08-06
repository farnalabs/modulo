"""Add deleted_at to teams for soft delete

Revision ID: 0068_add_teams_deleted_at
Revises: 0067_run_daily_facts
Create Date: 2026-08-06

Adds ``deleted_at`` to the ``teams`` table so team deletion follows the house
soft-delete pattern (``SoftDeleteMixin``) instead of a hard delete. The
``(organisation_id, name)`` unique CONSTRAINT is replaced with a PARTIAL unique
INDEX scoped to ``deleted_at IS NULL`` (the CostComponent/0066 pattern) so a
team's name can be reused after soft delete. Postgres-only partial-index DDL is
guarded with ``if _is_postgres(bind)``; on non-Postgres dev backends the plain
constraint remains (enforcement delegated to app-level name checks, matching
the CostComponent precedent).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_add_teams_deleted_at"
down_revision: str | None = "0067_run_daily_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("teams", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    if _is_postgres(bind):
        op.drop_constraint("uq_teams_organisation_name", "teams", type_="unique")
        op.create_index(
            "uq_teams_organisation_name",
            "teams",
            ["organisation_id", "name"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _is_postgres(bind):
        op.drop_index("uq_teams_organisation_name", table_name="teams")
        op.create_unique_constraint("uq_teams_organisation_name", "teams", ["organisation_id", "name"])
    op.drop_column("teams", "deleted_at")
