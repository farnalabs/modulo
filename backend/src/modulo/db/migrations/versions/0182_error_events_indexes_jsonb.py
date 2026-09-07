"""error_events: add status/level indexes, promote context_json to JSONB.

Revision ID: 0182_error_events_indexes_jsonb
Revises: 0181_org_api_keys_scope
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0182_error_events_indexes_jsonb"
down_revision: str | None = "0181_org_api_keys_scope"


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
