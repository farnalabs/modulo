"""improve-database(trigger_event): event-listing composite indexes.

Revision ID: 0277_trigger_events_listing_indexes
Revises: 0276_runs_autovacuum_enabled
Create Date: 2026-10-03

Covers ``backend/src/modulo/db/models/trigger_event.py``
(``trigger_events``), an area with no prior improve-database visit.
Existing coverage is single-column only:
``(organisation_id)`` (OrgScoped), ``(trigger_id)``, ``(run_id)`` and
``ix_trigger_events_received_at`` (0092 retention sweep). Two read paths
still filter on column sets with no composite to serve them:

* ``ix_trigger_events_org_trigger_created`` — the per-trigger event
  listing (``api/routes/triggers.py::list_trigger_events``,
  ``organisation_id = $1 AND trigger_id = $2`` with an optional
  ``validation_result = $3`` predicate, ``ORDER BY created_at DESC,
  id DESC LIMIT n+1``) ran on a bitmap-AND of the single-column org /
  trigger indexes plus a sort.
* ``ix_trigger_events_org_created`` — the org-wide event listings
  (``api/routes/admin_triggers.py``,
  ``api/mcp_server.py::_build_trigger_event_query``, both
  ``organisation_id = $1`` with optional ``trigger_type`` /
  ``validation_result`` predicates and the same recency ordering) had no
  composite at all; only the single-column org index plus a sort.

Deliberately NOT indexed: ``validation_result`` / ``trigger_type`` as
standalone or leading columns — both are low-cardinality equality
predicates always paired with the org (and usually trigger) prefix, so
they filter cheaply inside the composite range scan; a dedicated
low-cardinality btree would barely narrow. ``run_id`` already has its own
index for the dispatch dedup-expiry lookup
(``core/dispatch.py::_expire_webhook_dedup``), and ``received_at`` keeps
its sweep index for the 90-day retention job — neither is redundant with
the ``created_at``-ordered composites (different sort columns serve
different ORDER BY shapes). No ``raw_payload_hash`` index: hashes are
looked up by PK ``id`` (busy replay) or scanned with the run row, never
by hash predicate.

Additive indexes only — no column/table changes. Same
``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0155/0267/0271/0272
(Alembic wraps each revision in a transaction, so ``CONCURRENTLY`` is
unavailable). Both indexes lead on ``organisation_id`` (RLS org-isolated
table, so the tenant column must be the index prefix).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0277_trigger_events_listing_indexes"
down_revision: str | None = "0276_runs_autovacuum_enabled"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES = [
    (
        "ix_trigger_events_org_trigger_created",
        (
            "CREATE INDEX IF NOT EXISTS ix_trigger_events_org_trigger_created "
            'ON public."trigger_events" (organisation_id, trigger_id, created_at);'
        ),
    ),
    (
        "ix_trigger_events_org_created",
        (
            "CREATE INDEX IF NOT EXISTS ix_trigger_events_org_created "
            'ON public."trigger_events" (organisation_id, created_at);'
        ),
    ),
]


_DROPS = [
    "DROP INDEX IF EXISTS ix_trigger_events_org_trigger_created;",
    "DROP INDEX IF EXISTS ix_trigger_events_org_created;",
]


def upgrade() -> None:
    bind = op.get_bind()
    for _name, stmt in _INDEXES:
        bind.execute(text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in _DROPS:
        bind.execute(text(stmt))
