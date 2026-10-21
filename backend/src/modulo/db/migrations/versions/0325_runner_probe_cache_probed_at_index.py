"""Supporting index for the runner-probe retention sweep (FAR-591 / qa F8).

Revision ID: 0325_runner_probe_cache_probed_at_index
Revises: 0324_purge_eval_definitions
Create Date: 2026-10-21

``core/bundled_runner/health_probe.py::run_runner_health_probe`` calls
``crud/runner_probe.prune_stale_runner_probe_rows`` on EVERY probe tick (each
machine, 60s per ADR 029)::

    DELETE FROM runner_probe_cache WHERE probed_at < :cutoff

The sweep is deliberately cross-org (the probe runs as ``modulo_system`` and
prunes every org's decommissioned-machine rows), so its ONLY predicate is
``probed_at``. Before this migration the table's indexes were:

* ``uq_runner_probe_cache_org_machine (organisation_id, machine_id)``
* ``ix_runner_probe_cache_organisation_id (organisation_id)``
* ``ix_runner_probe_cache_machine_id (machine_id)``

None has ``probed_at`` as a leading column, so Postgres cannot range-scan the
purge and every tick degenerates into a sequential scan of the whole table just
to prove most rows are still young.

Index-shape note (why this is NOT a partial index): a ``postgresql_where``
predicate must be IMMUTABLE, so it cannot contain ``now()`` /
``CURRENT_TIMESTAMP`` (both STABLE) or a runtime bind parameter — the purge's
cutoff moves on every execution and can never be baked into an index predicate.
``probed_at`` is already ``NOT NULL``, so an immutable
``WHERE probed_at IS NOT NULL`` partial would add nothing over the plain btree.
The same shape decision was made for the sibling purge index on
``metrics_staging.recorded_at`` (migration 0304).

Additive index only — no column/table changes. Plain (non-CONCURRENT) create
follows the 0304/0268 convention: Alembic wraps each revision in a transaction
so ``CONCURRENTLY`` is unavailable, and this table holds at most one row per
(org, machine) — a tiny table, so the write lock is inconsequential. Declared on
the ORM model in the SAME change (``db/models/runner_probe_cache.py``) so model
and migration cannot drift.
"""

from __future__ import annotations

from alembic import op

revision: str = "0325_runner_probe_cache_probed_at_index"
down_revision: str | None = "0324_purge_eval_definitions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: Index name — asserted against the ``RunnerProbeCache`` model declaration by
#: ``tests/unit/db/test_migration_0325_runner_probe_cache_probed_at_index.py``.
INDEX_NAME: str = "ix_runner_probe_cache_probed_at"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "runner_probe_cache",
        ["probed_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="runner_probe_cache", if_exists=True)
