"""Per-table autovacuum tuning for ``runs``, ``checkpoints`` and ``checkpoint_writes`` (FAR-1442).

Revision ID: 0277_table_autovacuum_tuning
Revises: 0276_runs_autovacuum_enabled
Create Date: 2026-10-03

Postgres' autovacuum defaults are sized for SMALL tables. A relation only
becomes a vacuum candidate once it holds ``autovacuum_vacuum_threshold``
(default 50) PLUS ``autovacuum_vacuum_scale_factor`` (default 0.20, i.e.
**20% of its live rows**) as dead tuples; analyze waits for
``autovacuum_analyze_scale_factor`` (default 0.10, i.e. 10%). On Modulo's
large, fast-churning tables that 20% gate fires far too late, and the
cost-based throttle that is supposed to protect a shared server caps the
scan rate so low that autovacuum cannot catch up once it does fire.

Measured on production, 2026-10-03 (``pg_stat_user_tables`` /
``pg_total_relation_size``):

====================  ==========  ==========================
table                 size        lifetime deletes (``n_tup_del``)
====================  ==========  ==========================
``checkpoints``       9,436 MB    180,374
``checkpoint_writes`` 5,893 MB    **2,175,755**
``runs``              80 MB (1)   high insert/update churn + retention deletes
====================  ==========  ==========================

(1) ``runs`` after the 0276 VACUUM; while autovacuum was disabled it
measured 7.5 GB with 361,302 dead tuples against 10,411 live rows
(``last_autovacuum = never``) - the FAR-1419 incident 0276 repaired.

Why the defaults are wrong for this table shape
-----------------------------------------------

* **The 20% gate is a bloat budget measured in gigabytes.** At
  ``autovacuum_vacuum_scale_factor = 0.20`` the two multi-GB checkpoint
  tables are permitted to accumulate roughly a fifth of their live rows as
  dead tuples before a single vacuum starts: on a 9,436 MB table that is on
  the order of a couple of GB of reclaimable-but-unreclaimed heap, and on a
  table that has already deleted 2,175,755 rows it is hundreds of thousands
  of dead tuples per cycle. Lowering the factor to 0.02 cuts that dead-tuple
  budget tenfold, to ~2% of live rows (~190 MB of slack on ``checkpoints``,
  ~120 MB on ``checkpoint_writes``).
* **The default cost throttle is the other half of the failure.** The
  effective autovacuum scan rate is ``autovacuum_vacuum_cost_limit`` /
  ``autovacuum_vacuum_cost_delay``. The defaults resolve to
  ``vacuum_cost_limit`` (200 units) per 20 ms = **10,000 cost units/s**,
  which at ~10-20 units of work per 8 kB heap page is only ~4-8 MB/s - a
  single full pass over 9,436 MB therefore takes tens of minutes, so the
  next round of dead tuples arrives before the last pass finished. That is
  precisely the "autovacuum cannot keep up" shape behind the measured
  bloat. For the two multi-GB checkpoint tables this migration raises the
  per-wake budget 50x (200 -> 10,000) and cuts the sleep 10x (20 ms ->
  2 ms): 5,000,000 units/s, i.e. ~500x the default scan rate. The setting
  is still a throttle (the worker still sleeps every 2 ms once 10,000 units
  of work are done) - it is bounded, not unthrottled - but a full pass over
  9.4 GB now finishes in seconds rather than tens of minutes.
* **``runs`` needs a tighter factor, not cost tuning.** It is only 80 MB
  (measured post-VACUUM), so cost throttling is irrelevant at that size -
  what hurts is churn: it takes heavy insert/update traffic plus retention
  deletes (FAR-1419 measured 361,302 dead against 10,411 live while
  autovacuum was off). 0.05 makes the vacuum gate fire at
  ``50 + 0.05 * N`` dead tuples (~570 at the measured 10,411 live rows,
  versus ~2,132 at the default 0.20) and the analyze gate at
  ``50 + 0.02 * N`` (~258 changed rows, versus ~1,091 at the default 0.10),
  so planner stats on the hottest table in the product stay fresh.
* **``autovacuum_enabled = true`` is restated for ``runs`` in the same
  statement.** 0276 turned autovacuum back on after an out-of-band
  ``autovacuum_enabled=false`` reloption caused the bloat above; repeating
  it here keeps the migration correct under EITHER ``ALTER TABLE ... SET``
  semantics (see below) and makes this revision self-contained: even on a
  database where somehow only 0277 replayed, ``runs`` ends up enabled.

Merge semantics of ``ALTER TABLE ... SET (k = v)``
--------------------------------------------------

It **MERGES**: the statement adds/overwrites only the options it names and
leaves every other reloption on the relation untouched. Verified
empirically on PostgreSQL 16.15 (testcontainers, same major version as the
deploy target): a table carrying ``autovacuum_enabled=false`` that then gets
``SET (autovacuum_vacuum_scale_factor = 0.05)`` ends up carrying BOTH
``autovacuum_enabled=false`` and ``autovacuum_vacuum_scale_factor=0.05``.
``RESET`` is the exact inverse - it drops only the named options, and
``RESET`` of an option that was never set is accepted as a no-op. That
behaviour is pinned by
``tests/integration/test_migration_0277_table_autovacuum_tuning.py::TestAlterTableSetSemantics``.
The migration does not merely rely on it: ``autovacuum_enabled = true`` is
included in ``runs``' SET list, so ``runs`` is correctly enabled whether
SET merges (it merges) or replaces.

The two checkpoint tables are runtime-owned, so their ALTER is
existence-gated
------------------------------------------------------------------------

``checkpoints`` and ``checkpoint_writes`` are NOT created by alembic - they
are created at application startup by ``ModuloPostgresSaver.setup()``
(``_MIGRATION_SQL``), which runs in the FastAPI lifespan AFTER alembic has
finished (``deploy/fly/entrypoint.sh`` runs ``alembic upgrade heads`` before
``uvicorn`` starts). On a brand-new database this revision therefore meets
a schema in which the checkpoint tables do not exist yet, so each ALTER is
guarded by ``to_regclass`` and **RAISEs a NOTICE** when it skips - the skip
is observable in the migration log, never silent. On every already-deployed
database (tables created at first boot long before this revision ran) the
guard passes and the tuning lands.

**Known limitation, stated explicitly rather than papered over:** on an
install whose alembic chain reaches 0277 before the application's first
boot, ``runs`` is tuned but ``checkpoints``/``checkpoint_writes`` are not -
alembic records the revision as applied and never replays it. Closing that
gap means carrying the same statements in
``ModuloPostgresSaver._MIGRATION_SQL`` (which runs idempotently on every
startup), which is application code outside this migration's scope.

Shape and idempotency
---------------------

Every statement is a plain reloption write inside the migration
transaction - no side connections - so the revision rehearses cleanly under
``ALEMBIC_REHEARSAL=1``. Re-running is a no-op (re-writing a reloption with
its existing value changes nothing), and a fresh database that does have
the tables simply re-states defaults-plus-tuning.

Downgrade RESETs exactly the options this revision added (``runs``: the two
scale factors; the checkpoint tables: both scale factors plus the two cost
knobs), which is a no-op when an option is absent. It deliberately does NOT
``RESET autovacuum_enabled`` on ``runs``: that reloption belongs to 0276,
so after downgrading to 0276 the schema must equal what 0276 left -
``autovacuum_enabled=true`` with no tuning - and re-disabling autovacuum is
the defect 0276 exists to repair.
"""

from __future__ import annotations

from alembic import op

revision: str = "0277_table_autovacuum_tuning"
down_revision: str | None = "0276_runs_autovacuum_enabled"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ``runs`` is migration-owned (created and maintained by this chain), so it
# always exists when this revision runs. autovacuum_enabled=true is restated
# alongside the tuning so the statement is correct under either SET
# semantics (merge or replace).
_RUNS_SET = (
    'ALTER TABLE public."runs" SET ('
    "autovacuum_enabled = true, "
    "autovacuum_vacuum_scale_factor = 0.05, "
    "autovacuum_analyze_scale_factor = 0.02);"
)
_RUNS_RESET = 'ALTER TABLE public."runs" RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor);'


def _checkpoint_set(table: str) -> str:
    """Existence-gated tuning statement for a runtime-created table.

    ``to_regclass`` is NULL when the table has not been created yet (a fresh
    database: alembic runs before ``ModuloPostgresSaver.setup()``), so the
    ALTER is skipped with a NOTICE instead of failing the whole chain.
    """
    return (
        "DO $$ BEGIN "
        f"IF to_regclass('public.{table}') IS NOT NULL THEN "
        f'ALTER TABLE public."{table}" SET ('
        "autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_analyze_scale_factor = 0.01, "
        "autovacuum_vacuum_cost_limit = 10000, "
        "autovacuum_vacuum_cost_delay = 2); "
        "ELSE "
        f"RAISE NOTICE '0277_table_autovacuum_tuning: skipping {table} - "
        "table does not exist yet (created by ModuloPostgresSaver.setup() "
        "at application startup)'; "
        "END IF; END $$;"
    )


def _checkpoint_reset(table: str) -> str:
    """Mirror of :func:`_checkpoint_set`: drop only what the upgrade added."""
    return (
        "DO $$ BEGIN "
        f"IF to_regclass('public.{table}') IS NOT NULL THEN "
        f'ALTER TABLE public."{table}" RESET ('
        "autovacuum_vacuum_scale_factor, "
        "autovacuum_analyze_scale_factor, "
        "autovacuum_vacuum_cost_limit, "
        "autovacuum_vacuum_cost_delay); "
        "END IF; END $$;"
    )


# Checkpoint tables, in a fixed order so the migration log is deterministic.
_CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_writes")


def upgrade() -> None:
    op.execute(_RUNS_SET)
    for table in _CHECKPOINT_TABLES:
        op.execute(_checkpoint_set(table))


def downgrade() -> None:
    op.execute(_RUNS_RESET)
    for table in _CHECKPOINT_TABLES:
        op.execute(_checkpoint_reset(table))
