"""Add partial indexes for polling, ongoing, and agent_signal tick scans.

Revision ID: 0182_triggers_add_polling_ongoing_agent_signal_indexes
Revises: 0181_org_api_keys_scope
Create Date: 2026-09-07

``0167_add_hot_query_indexes`` added ``ix_triggers_due_cron`` for the
cron tick scan but the equivalent hot-path lookups for ``polling``,
``ongoing``, and ``agent_signal`` trigger types were not covered. Each
of these runs every tick per org and currently full-scans every org's
triggers through the single-column ``ix_triggers_next_fire_at`` or
``ix_triggers_organisation_id`` indexes.

Add partial indexes that narrow to the exact predicate set each tick
scan uses, keeping the planner's index scan tight.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0182_triggers_add_polling_ongoing_agent_signal_indexes"
down_revision: str | None = "0181_org_api_keys_scope"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_triggers_due_polling",
        "triggers",
        ["organisation_id", "next_fire_at"],
        postgresql_where=sa.text("trigger_type = 'polling' AND active IS TRUE AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_triggers_due_ongoing",
        "triggers",
        ["organisation_id"],
        postgresql_where=sa.text("trigger_type = 'ongoing' AND active IS TRUE AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_triggers_agent_signal_active",
        "triggers",
        ["organisation_id"],
        postgresql_where=sa.text("trigger_type = 'agent_signal' AND active IS TRUE AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_triggers_agent_signal_active", table_name="triggers")
    op.drop_index("ix_triggers_due_ongoing", table_name="triggers")
    op.drop_index("ix_triggers_due_polling", table_name="triggers")
