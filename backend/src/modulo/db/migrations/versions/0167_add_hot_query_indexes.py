"""Add hot-path composite / partial indexes on runs and triggers.

Revision ID: 0167_add_hot_query_indexes
Revises: 0166_promote_uuid_fk_columns
Create Date: 2026-08-29

* ``ix_triggers_due_cron`` — partial index
  ``triggers(organisation_id, next_fire_at) WHERE trigger_type = 'cron'
  AND active IS TRUE AND deleted_at IS NULL AND cron_expression IS NOT NULL``.
  ``_process_due_cron_scan`` (cron_helpers.fire_due_triggers) runs every tick
  per org over exactly these constant predicates plus RLS-injected
  ``organisation_id``; without the partial index each tick full-scans every
  org's triggers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0167_add_hot_query_indexes"
down_revision: str | None = "0166_promote_uuid_fk_columns"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_triggers_due_cron",
        "triggers",
        ["organisation_id", "next_fire_at"],
        postgresql_where=sa.text(
            "trigger_type = 'cron' AND active IS TRUE AND deleted_at IS NULL AND cron_expression IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_triggers_due_cron", table_name="triggers")
