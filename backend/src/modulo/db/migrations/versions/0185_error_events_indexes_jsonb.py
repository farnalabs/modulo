"""error_events: add status/level partial indexes.

Renumber note: originally authored as 0182_error_events_indexes_jsonb
(down_revision 0181_org_api_keys_scope), then renumbered to 0183 when main
merged 0182_hitl_claims_active_sweep_indexes. While this branch was in
flight, main advanced to 0183_triggers_add_polling_ongoing_agent_signal_indexes
and 0184_trigger_events_indexes_and_type_check, so the 0183 prefix now
collides. This migration is renumbered to 0185 and re-parented onto the
real main head 0184_trigger_events_indexes_and_type_check to keep a single head.

History note: an earlier draft also ALTERed error_events.context_json from
JSON to JSONB. That was dropped because 0147_json_to_jsonb_standardize (and
its 0148 revision) already converts ("error_events", "context_json") to jsonb
on main, so the ALTER would be a guaranteed no-op on every real database (and
its downgrade would regress 0147's codebase-wide jsonb standard). The column
is already jsonb by the time this migration runs; only the two indexes remain.

Indexes:
- ix_error_events_status_new: (organisation_id, created_at) WHERE status = 'new'.
  Keys duplicate ix_error_events_org_created_at (0155), but the partial
  predicate keeps the index small (only the live 'new' rows), which is the
  sole value-add over 0155 - mirroring 0182's partial-index rationale.
- ix_error_events_org_level: (organisation_id, level, created_at) for
  org-scoped level filtering.

Revision ID: 0185_error_events_indexes_jsonb
Revises: 0184_trigger_events_indexes_and_type_check
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0185_error_events_indexes_jsonb"
down_revision: str | None = "0184_trigger_events_indexes_and_type_check"


def upgrade() -> None:
    # --- indexes -----------------------------------------------------------
    op.create_index(
        "ix_error_events_status_new",
        "error_events",
        ["organisation_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'new'"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_error_events_org_level",
        "error_events",
        ["organisation_id", "level", "created_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_error_events_org_level", table_name="error_events", if_exists=True)
    op.drop_index("ix_error_events_status_new", table_name="error_events", if_exists=True)
