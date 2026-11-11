"""Add (organisation_id, deleted_at) partial indexes on remaining soft-delete tables.

Revision ID: 0168_add_soft_delete_org_indexes
Revises: 0167_add_hot_query_indexes
Create Date: 2026-08-31

``0125_soft_delete_lookup_indexes`` added the partial composite index
``(organisation_id, deleted_at) WHERE deleted_at IS NULL`` to eleven
OrgScoped + SoftDeleteMixin tables, but four such tables were omitted
(``teams``, ``triggers``, ``cost_components``, ``notification_endpoints``)
because their ``deleted_at`` column was introduced in the ``0108``/``0110``
schema-regeneration pass after ``0125`` landed, or they were simply missed.

Every active-row read against these tables filters on
``organisation_id = $1 AND deleted_at IS NULL`` (RLS injects the org
predicate; the SoftDeleteMixin/query layer injects ``deleted_at IS NULL``).
Without the partial index the planner falls back to the single-column
``ix_<table>_organisation_id`` index scan plus a heap filter on
``deleted_at``. This adds the same partial composite index that ``0125``
established for the peer tables, keeping index-only scans possible.

Postgres-only concern: ``postgresql_where`` is ignored by the deprecated
SQLite / MariaDB backends, where the index is created without the predicate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0168_add_soft_delete_org_indexes"
down_revision: str | None = "0167_add_hot_query_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# OrgScoped + SoftDeleteMixin tables still missing a (organisation_id, deleted_at)
# partial lookup index. organisation_id is already indexed by the base mixin.
_TABLES = (
    "teams",
    "triggers",
    "cost_components",
    "notification_endpoints",
)

_DELETED_AT_WHERE = sa.text("deleted_at IS NULL")


def _index_name(table: str) -> str:
    return f"ix_{table}_organisation_id_deleted_at"


def upgrade() -> None:
    for table in _TABLES:
        op.create_index(
            _index_name(table),
            table,
            ["organisation_id", "deleted_at"],
            postgresql_where=_DELETED_AT_WHERE,
            if_not_exists=True,
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(_index_name(table), table_name=table, if_exists=True)
