"""Require a name on every trigger: backfill NULL names, then ``name`` NOT NULL (FAR-2227).

Revision ID: 0339_triggers_name_not_null
Revises: 0338_environment_profile_max_node_seconds
Create Date: 2026-10-29

``triggers.name`` (0203, FAR-681) was nullable, so triggers created through
the UI / MCP were identified only by UUID. FAR-2227 makes the name a required
part of every trigger. No alpha release had been installed when this landed,
so the change is made cleanly: no deprecation window, no compatibility shim.

Upgrade, in order:

1. Backfill every NULL name - live AND soft-deleted rows, so NOT NULL holds
   for the whole table - with the deterministic value
   ``<trigger_type>-<first 8 hex chars of the id>``. Where that candidate
   would collide with a live trigger already holding the name on the same
   ``(organisation_id, pipeline_id)`` (the ``uq_triggers_org_pipeline_name``
   identity), the row falls back to the full 32-char hex id, which is unique
   by construction.
2. ``ALTER ... SET NOT NULL`` (batch mode, so SQLite - which cannot ALTER a
   column's nullability in place - recreates the table; Postgres emits a
   plain ``ALTER COLUMN``).

The partial unique index keeps its ``name IS NOT NULL`` predicate: it is
harmless once the column is NOT NULL and keeps the index definition identical
to 0203, so the downgrade never has to rebuild it.

Downgrade makes the column nullable again; backfilled names are left in place
(they are valid names and dropping them would lose information).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0339_triggers_name_not_null"
down_revision: str | None = "0338_environment_profile_max_node_seconds"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: Literal SQL only (no f-string/format-built SQL - migration-fstring-sql
#: semgrep rule). ``CAST(id AS TEXT)`` renders a uuid on Postgres and a
#: CHAR(32) on SQLite; stripping dashes makes both a 32-char hex string.
_BACKFILL_NULL_NAMES = (
    "UPDATE triggers AS t SET name = CASE WHEN EXISTS ("
    "SELECT 1 FROM triggers o "
    "WHERE o.organisation_id = t.organisation_id "
    "AND o.pipeline_id = t.pipeline_id "
    "AND o.deleted_at IS NULL "
    "AND o.name = t.trigger_type || '-' || substr(replace(CAST(t.id AS TEXT), '-', ''), 1, 8)"
    ") THEN t.trigger_type || '-' || replace(CAST(t.id AS TEXT), '-', '') "
    "ELSE t.trigger_type || '-' || substr(replace(CAST(t.id AS TEXT), '-', ''), 1, 8) END "
    "WHERE t.name IS NULL"
)


def upgrade() -> None:
    op.execute(sa.text(_BACKFILL_NULL_NAMES))
    with op.batch_alter_table("triggers") as batch_op:
        batch_op.alter_column("name", existing_type=sa.String(255), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("triggers") as batch_op:
        batch_op.alter_column("name", existing_type=sa.String(255), nullable=True)
