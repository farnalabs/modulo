"""error_events: add status/level indexes, promote context_json to JSONB.

Renumber note: originally authored as 0182_error_events_indexes_jsonb
(down_revision 0181_org_api_keys_scope). Main merged 0182_hitl_claims_
active_sweep_indexes first, so this migration was renumbered to 0183 to
avoid a two-head collision on the 0182 prefix and re-parented onto the
real main head 0182_hitl_claims_active_sweep_indexes.

Revision ID: 0183_error_events_indexes_jsonb
Revises: 0182_hitl_claims_active_sweep_indexes
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0183_error_events_indexes_jsonb"
down_revision: str | None = "0182_hitl_claims_active_sweep_indexes"


def upgrade() -> None:
    # --- indexes -----------------------------------------------------------
    op.create_index(
        "ix_error_events_status_new",
        "error_events",
        ["organisation_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'new'"),
    )
    op.create_index(
        "ix_error_events_org_level",
        "error_events",
        ["organisation_id", "level", "created_at"],
        unique=False,
    )

    # --- column type: JSON -> JSONB ----------------------------------------
    op.execute('ALTER TABLE public."error_events" ALTER COLUMN "context_json" TYPE jsonb USING "context_json"::jsonb')


def downgrade() -> None:
    op.drop_index("ix_error_events_org_level", table_name="error_events")
    op.drop_index("ix_error_events_status_new", table_name="error_events")

    op.execute('ALTER TABLE public."error_events" ALTER COLUMN "context_json" TYPE json USING "context_json"::json')
