"""improve-database(webhook): replay + org-scoped expiry lookup indexes.

Revision ID: 0268_webhook_lookup_expiry_indexes
Revises: 0267_notification_hot_query_indexes
Create Date: 2026-09-29

Covers ``backend/src/modulo/db/models/webhook.py`` (``webhook_dedup_hashes``,
``webhook_payloads``), an area with no prior improve-database visit. Existing
coverage is single-column only (0003/0110/0142): ``(trigger_id)``,
``(trigger_event_id)``, ``(organisation_id)``, ``(expires_at)`` plus the
``uq_webhook_dedup_trigger_hash (trigger_id, payload_hash)`` unique. Three
read paths still filter on column pairs with no composite to serve them:

* ``ix_webhook_payloads_trigger_event_org`` — the replay read
  (``api/routes/webhooks.py``) filters
  ``trigger_event_id = $1 AND organisation_id = $2``; only the
  single-column ``ix_webhook_payloads_trigger_event_id`` existed.
* ``ix_webhook_dedup_hashes_org_expires`` — the org-scoped expiry scan
  (``core/housekeeping.py::_scan_expired_webhook_dedups``) filters
  ``organisation_id = $1 AND expires_at < $2``; the global purge
  (``trigger_engine.cleanup_expired_dedup_hashes``, ``WHERE expires_at``)
  is already served by ``ix_webhook_dedup_hashes_expires_at``.
* ``ix_webhook_payloads_org_expires`` — symmetric org-scoped arm for the
  payload expiry sweep; the global ``WHERE expires_at`` purge keeps its
  existing single-column index.

The hot dedup point lookup
(``WHERE trigger_id = $1 AND payload_hash = $2 AND expires_at > now()``)
needs nothing: the ``uq_webhook_dedup_trigger_hash`` unique index already
serves the equality prefix. RLS (``rls_org_isolation``, 0110) is present on
both tables; every new index leads on the tenant column where the query is
tenant-scoped.

Additive indexes only — no column/table changes. Same
``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0155/0267 (Alembic wraps each
revision in a transaction, so ``CONCURRENTLY`` is unavailable).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0268_webhook_lookup_expiry_indexes"
down_revision: str | None = "0267_notification_hot_query_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES = [
    (
        "ix_webhook_payloads_trigger_event_org",
        (
            "CREATE INDEX IF NOT EXISTS ix_webhook_payloads_trigger_event_org "
            'ON public."webhook_payloads" (trigger_event_id, organisation_id);'
        ),
    ),
    (
        "ix_webhook_dedup_hashes_org_expires",
        (
            "CREATE INDEX IF NOT EXISTS ix_webhook_dedup_hashes_org_expires "
            'ON public."webhook_dedup_hashes" (organisation_id, expires_at);'
        ),
    ),
    (
        "ix_webhook_payloads_org_expires",
        (
            "CREATE INDEX IF NOT EXISTS ix_webhook_payloads_org_expires "
            'ON public."webhook_payloads" (organisation_id, expires_at);'
        ),
    ),
]


_DROPS = [
    "DROP INDEX IF EXISTS ix_webhook_payloads_trigger_event_org;",
    "DROP INDEX IF EXISTS ix_webhook_dedup_hashes_org_expires;",
    "DROP INDEX IF EXISTS ix_webhook_payloads_org_expires;",
]


def upgrade() -> None:
    bind = op.get_bind()
    for _name, stmt in _INDEXES:
        bind.execute(text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in _DROPS:
        bind.execute(text(stmt))
