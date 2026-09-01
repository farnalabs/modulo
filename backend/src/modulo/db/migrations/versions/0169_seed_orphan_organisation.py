"""Seed the orphan organisation row backing public error ingest.

Revision ID: 0169_seed_orphan_organisation
Revises: 0168_add_soft_delete_org_indexes
Create Date: 2026-09-01

The public error-ingest endpoint (``POST /api/v1/errors/ingest/public``) pins
its transaction to the nil-UUID organisation
(00000000-0000-0000-0000-000000000000) so the org-only RLS policies on
``error_events``/``error_groups`` pass WITH CHECK for unattributed frontend
errors. ``error_events.organisation_id`` carries a HARD foreign key to
``organisations.id``, so that nil-UUID org must exist as a real row — or every
public-ingest INSERT fails the FK while ``ingest_batch`` swallows the
per-event errors, yielding a false-success 201 with nothing persisted.

This row is a write-only orphan partition: RLS org-only policies keep it
invisible to every tenant session, and nothing (users, teams, pipelines)
links to it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0169_seed_orphan_organisation"
down_revision: str | None = "0168_add_soft_delete_org_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ORPHAN_ORG_ID = "00000000-0000-0000-0000-000000000000"

# NOT NULL columns without server defaults (name, slug, settings_json) are
# supplied explicitly; status / created_at / authz_enforce / triggers_paused /
# guardrails_kill_switch / org_cumulative_spend_cents take their server
# defaults. ON CONFLICT DO NOTHING (no target) keeps the seed idempotent and
# sidesteps both the PK and the partial-unique slug index.
_INSERT_ORPHAN_ORG = (
    "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
    "VALUES (:id, :name, :slug, '{}', '{}') "
    "ON CONFLICT DO NOTHING"
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(_INSERT_ORPHAN_ORG),
        {
            "id": _ORPHAN_ORG_ID,
            "name": "Orphan (unattributed errors)",
            "slug": "orphan-unattributed-errors",
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM organisations WHERE id = :id"),
        {"id": _ORPHAN_ORG_ID},
    )
