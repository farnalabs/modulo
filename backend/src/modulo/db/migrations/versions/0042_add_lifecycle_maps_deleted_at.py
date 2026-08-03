"""Add deleted_at to lifecycle_maps for soft delete

Revision ID: 0022_add_lifecycle_maps_deleted_at
Revises: 0021_add_pipelines_deleted_at
Create Date: 2026-07-22

DRIFT-TOLERANT: pre-squash staging databases may be missing the entire
``lifecycle_maps`` table (created later by ``0064_reconcile_staging_schema``).
Skip the ``add_column`` when the table does not exist instead of hard-failing
``alembic upgrade heads`` and blocking deploys. On a healthy schema the table
exists and behaviour is unchanged.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0022_add_lifecycle_maps_deleted_at"
down_revision = "0021_add_pipelines_deleted_at"
branch_labels = None
depends_on = None


def _column_exists(bind: Any, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _table_exists(bind: Any, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _warn(message: str) -> None:
    print(message)  # noqa: T201 - printed warning surfaces in deploy logs


def upgrade() -> None:
    if not _table_exists(op.get_bind(), "lifecycle_maps"):
        _warn("SKIP lifecycle_maps.deleted_at: table missing (drift-tolerant)")
        return
    if _column_exists(op.get_bind(), "lifecycle_maps", "deleted_at"):
        _warn("SKIP lifecycle_maps.deleted_at: column already present (drift-tolerant)")
        return
    op.add_column("lifecycle_maps", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("lifecycle_maps", "deleted_at")
