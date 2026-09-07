"""Add composite indexes and trigger_type CHECK on trigger_events.

Revision ID: 0183_trigger_events_indexes_and_type_check
Revises: 0182_triggers_add_polling_ongoing_agent_signal_indexes
Create Date: 2026-09-07

* ``ix_trigger_events_trigger_org_created`` — covering composite index
  for the per-trigger event listing (``WHERE trigger_id = $1 AND
  organisation_id = $2 ORDER BY created_at DESC, id DESC``) used by the
  trigger detail page. Without it, a filesort on every page load.

* ``ix_trigger_events_org_created`` — covering composite index for the
  admin/org-wide event listing (``WHERE organisation_id = $1 ORDER BY
  created_at DESC, id DESC``).

* ``ck_trigger_events_trigger_type`` — CHECK constraint on
  ``trigger_type``. The column is a denormalised copy of the parent
  trigger's type; without a CHECK a bug can insert any arbitrary string.
  Mirrors ``ck_triggers_type`` on the ``triggers`` table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0183_trigger_events_indexes_and_type_check"
down_revision: str | None = "0182_triggers_add_polling_ongoing_agent_signal_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_trigger_events_trigger_org_created",
        "trigger_events",
        ["trigger_id", "organisation_id", "created_at", "id"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_trigger_events_org_created",
        "trigger_events",
        ["organisation_id", "created_at", "id"],
        postgresql_using="btree",
    )
    op.execute(
        sa.text(
            "ALTER TABLE trigger_events ADD CONSTRAINT ck_trigger_events_trigger_type "
            "CHECK (trigger_type IN "
            "('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'slack_app_mention'))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE trigger_events DROP CONSTRAINT IF EXISTS ck_trigger_events_trigger_type"))
    op.drop_index("ix_trigger_events_org_created", table_name="trigger_events")
    op.drop_index("ix_trigger_events_trigger_org_created", table_name="trigger_events")
