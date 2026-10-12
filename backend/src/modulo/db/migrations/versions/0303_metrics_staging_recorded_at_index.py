"""Supporting index for the metrics_staging purge (FAR-1717).

Revision ID: 0303_metrics_staging_recorded_at_index
Revises: 0302_runs_run_grouping
Create Date: 2026-10-12

``core/product_analytics/metrics_dump.py::_purge_staging`` deletes
``metrics_staging`` rows on EVERY ``metrics_dump`` tick (``*/10 * * * *``)::

    DELETE FROM metrics_staging WHERE recorded_at < :cutoff

with ``:cutoff`` = max(start-of-day(min(watermark + 1 day, today)),
``now - 30 days``). Before this migration the only index on the table was
``ix_metrics_staging_org_recorded_at (organisation_id, recorded_at)``: its
leading column is unconstrained by the purge, so Postgres cannot range-scan
``recorded_at`` through it and every tick degenerates into a scan of the whole
table just to prove most rows are still young.

Index-shape note (why this is NOT a partial index): a ``postgresql_where``
predicate in Postgres must be IMMUTABLE, so it cannot contain ``now()`` /
``CURRENT_TIMESTAMP`` (both STABLE) or a runtime bind parameter — the purge's
cutoff moves on every execution and can never be baked into an index predicate.
The same shape is used by the other time-range purges in this repo
(``ix_webhook_dedup_hashes_expires_at`` for the ``WHERE expires_at < :cutoff``
purge, migration 0268): the immutable-part-only partial index
(``WHERE recorded_at IS NOT NULL``) would add nothing over a plain index here
because the column is already ``NOT NULL``. So the matching index is the plain
btree on the purge's range key, and this migration adds exactly that.

The ``_aggregate_staged_events`` read added alongside the purge is org-scoped
(``organisation_id`` equality + ``recorded_at`` range), so it stays on the
existing ``ix_metrics_staging_org_recorded_at`` and needs nothing new here.

Additive index only — no column/table changes. Plain (non-CONCURRENT) create
follows the 0128/0155/0267/0271/0278 convention: Alembic wraps each revision in
a transaction so ``CONCURRENTLY`` is unavailable, and ``metrics_staging`` is a
low-volume staging table (the lock is inconsequential). Declared on the ORM
model in the SAME change (``db/models/metrics_staging.py``) so model and
migration cannot drift.
"""

from __future__ import annotations

from alembic import op

revision: str = "0303_metrics_staging_recorded_at_index"
down_revision: str | None = "0302_runs_run_grouping"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: Index name — asserted against the ``MetricsStaging`` model declaration by
#: ``tests/unit/db/test_migration_0303_metrics_staging_recorded_at_index.py``.
INDEX_NAME: str = "ix_metrics_staging_recorded_at"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "metrics_staging",
        ["recorded_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="metrics_staging", if_exists=True)
