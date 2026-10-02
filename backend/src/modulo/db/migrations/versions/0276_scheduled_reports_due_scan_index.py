"""improve-database(scheduled_report): due-scan index + active default.

Revision ID: 0276_scheduled_reports_due_scan_index
Revises: 0275_run_cancel_reason_vocabulary
Create Date: 2026-10-02

Covers ``backend/src/modulo/db/models/scheduled_report.py``
(``scheduled_reports``), an area with no prior improve-database visit.
Existing coverage is single-column only (0005/0110/0142: ``report_type``,
``organisation_id``, ``created_by``) plus the soft-delete partial index
``ix_scheduled_reports_organisation_id_deleted_at`` (0132), the jsonb
promotion (0147), and the audit columns themselves (0132). Four gaps remain:

* ``ix_scheduled_reports_due`` — the per-tick due-scan
  (``core/cron_helpers.py::_process_due_report_scan``,
  ``WHERE active IS TRUE AND next_send_at IS NOT NULL AND next_send_at <=
  now()``) has no supporting index: every tick scans via the
  single-column org/report_type indexes. The partial composite
  ``(organisation_id, next_send_at)`` narrows to exactly the due
  predicate (leading on ``organisation_id`` — the table is RLS
  org-isolated, so the tenant column is the index prefix) and also covers
  the fire-path re-check (``id = $1 AND organisation_id = $2 AND
  next_send_at <= now()``) as a secondary access path.
* ``active`` server default — the model declared only a client-side
  ``default=True`` (unlike ``Trigger.active``'s ``server_default``), so a
  raw-SQL / rolling-deploy insert omitting ``active`` trips the NOT NULL
  constraint. ``SET DEFAULT true`` aligns the DB with the model; the ORM
  change ships in the same commit.

The ORM model additionally aligns with already-shipped DB contracts (no
migration needed): ``SoftDeleteMixin`` + ``deleted_by``/``updated_by``
(0132 added the columns, FKs, and partial index but the model never
declared them, so ``delete_scheduled_report`` hard-deleted while the DB
convention is soft delete), and the JSONB variant for ``config_json`` /
``recipient_config`` (0147 promoted the columns to jsonb; the codebase
JSON standard per ``agent.py``).

Additive only — one index plus a column default. Same non-concurrent
``CREATE INDEX`` pattern as 0128/0155/0267/0271 (Alembic wraps each
revision in a transaction, so ``CONCURRENTLY`` is unavailable). ``SET
DEFAULT`` is a catalog-only change (no table rewrite, no lock beyond a
brief share-update-exclusive). The default change is Postgres-only with a
dialect guard (0129 pattern); the index uses ``postgresql_where`` so it
is portable (0183 pattern).

Deferred (recorded, not implemented): a ``report_type`` CHECK. Report
types are an open registry (``register_report_type`` — "cost" and
"quality" today, pluggable tomorrow); a hardcoded vocabulary CHECK would
reject rows for future registered types.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0276_scheduled_reports_due_scan_index"
down_revision: str | None = "0275_run_cancel_reason_vocabulary"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_scheduled_reports_due",
        "scheduled_reports",
        ["organisation_id", "next_send_at"],
        postgresql_where=sa.text("active IS TRUE AND next_send_at IS NOT NULL AND deleted_at IS NULL"),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("ALTER TABLE scheduled_reports ALTER COLUMN active SET DEFAULT true"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("ALTER TABLE scheduled_reports ALTER COLUMN active DROP DEFAULT"))
    op.drop_index("ix_scheduled_reports_due", table_name="scheduled_reports")
