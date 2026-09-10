"""Add ``collection_install_id`` denormalised columns to entity tables (FAR-762).

Revision ID: 0210_collection_install_id_columns
Revises: 0209_community_gate
Create Date: 2026-09-10

The collection install/uninstall feature (FAR-762) stamps the owning install id
onto every entity it materialises (schemas, agents, pipelines) so uninstall can
determine which entities to delete/detach and so callers can resolve an entity's
provenance in O(1). The ORM models (``Schema``/``Agent``/``Pipeline``) and the
install/uninstall services already read and write ``collection_install_id``, but
the column was never created by any migration — migration ``0207`` intentionally
introduces only the ``collection_install_entity`` child for per-entity audit rows
and omits the denormalised column. This migration closes that drift so the ORM
metadata and the applied schema agree.

Columns are nullable (an entity may exist outside any collection install) and
indexed to mirror the ORM ``index=True`` mapping and to speed the
install-id → entities reverse lookup.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0210_collection_install_id_columns"
down_revision: str | None = "0209_community_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTITY_TABLES = ("schemas", "agents", "pipelines")
_COLUMN = "collection_install_id"


def upgrade() -> None:
    for table in _ENTITY_TABLES:
        op.add_column(
            table,
            sa.Column(_COLUMN, sa.Uuid(), nullable=True),
        )
        op.create_index(
            f"ix_{table}_{_COLUMN}",
            table,
            [_COLUMN],
        )


def downgrade() -> None:
    for table in reversed(_ENTITY_TABLES):
        op.drop_index(f"ix_{table}_{_COLUMN}", table_name=table)
        op.drop_column(table, _COLUMN)
