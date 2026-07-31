"""Add composite index for error_events windowed count lookups

AlertEngine._count_events_in_window() filters error_events by
(organisation_id, fingerprint, created_at) on every rule evaluation.
The existing ix_error_events_organisation_id index cannot serve that
query efficiently, so add a composite index covering all three columns.
get_error_events_by_group / count_error_events_by_group (fingerprint
lookups ordered by created_at) also benefit.

Revision ID: 0030_add_error_events_window_index
Revises: 0029_fix_expiry_fields_non_null
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op

revision = "0030_add_error_events_window_index"
down_revision = "0029_fix_expiry_fields_non_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_error_events_org_fingerprint_created_at"),
        "error_events",
        ["organisation_id", "fingerprint", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_error_events_org_fingerprint_created_at"),
        table_name="error_events",
    )
