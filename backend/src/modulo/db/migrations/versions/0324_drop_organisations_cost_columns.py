"""Drop the retired ``organisations`` cost columns + CHECK (FAR-1902, contract half).

Revision ID: 0324_drop_organisations_cost_columns
Revises: 0323_pipeline_snapshots_substantive_version_index
Create Date: 2026-10-20

This is the CONTRACT half of the FAR-1833 expand/contract cutover. Migration
0321 created ``organisation_cost_state``, backfilled one row per organisation
from these three ``organisations`` columns, and left the source columns in place
so that a rolling deploy could never serve old code against a dropped column.
FAR-1833 (PR #1907) then moved every reader and writer onto the cost-state table
under the per-org cost-state advisory lock; it deliberately did NOT drop the
columns, because migrations run once BEFORE machines roll and the drop would
have shared a deploy with the code that stopped using them.

That release has been live since 2026-10-20 (the rollout window this split
exists to remove has passed), so the columns and the CHECK that references one
of them are now dead weight:

* ``max_run_cost_cents`` — per-run hard ceiling, now
  ``organisation_cost_state.max_run_cost_cents``.
* ``spend_ceiling_cents`` — org lifetime budget, now
  ``organisation_cost_state.spend_ceiling_cents``.
* ``org_cumulative_spend_cents`` — running consumed total, now
  ``organisation_cost_state.org_cumulative_spend_cents``.
* ``ck_organisations_cum_spend`` — the ``org_cumulative_spend_cents >= 0``
  CHECK (added by 0151, VALIDATE-d online). It is dropped explicitly first so
  the intent is visible in the deploy log rather than implied by PostgreSQL's
  column-dependency cascade; the equivalent guard lives on as
  ``ck_organisation_cost_state_cum_spend`` on the surviving table.

The ``Organisation`` ORM mapping loses the same three columns + CHECK in THIS
change: the migrated schema and the ORM metadata must match
(``tests/integration/test_initial_migration.py::test_migrated_schema_matches_orm_metadata``),
so a model left declaring a dropped column or a dropped CHECK is drift.

BOUNDED DDL (the 0129/0215 precedent). Every ``ALTER TABLE organisations``
statement here is an ``ACCESS EXCLUSIVE`` lock request. On a busy production
database an unbounded request can queue behind live traffic and wedge the whole
upward migration chain, so each statement runs with a transaction-scoped
``SET LOCAL lock_timeout`` inside a per-attempt SAVEPOINT with a bounded retry
(:func:`_ddl_with_retry`, the shape of
``0215_drop_runs_blob_columns._drop_with_retry``): a refused attempt rolls back
only its own savepoint and retries; the final failure propagates so the
migration aborts loudly and release.sh's bounded migrator owns the retry.

The downgrade RESTORES THE COLUMNS AND THEIR VALUES from
``organisation_cost_state``, which is the AUTHORITATIVE store after FAR-1833
(every reader and writer moved there under the per-org cost-state advisory
lock). It is therefore not a byte-for-byte inverse of the pre-cutover state,
and this module does not claim it is: a value that an old-code instance wrote
to ``organisations`` only, during the rollout window this split exists to
remove, is SUPERSEDED by the cost-state value. That precedence is the
cutover's intent, not data loss. That cost-state table is still present at
this revision (0321 only drops it in its OWN downgrade, which runs after this
one), so the restore source is guaranteed.

The one class the drop must not lose is an organisation with NO cost-state
row. 0321 backfills only the organisations that existed when it ran, and the
application never creates the row eagerly (a missing row means "no ceilings,
``0`` spent"; the row appears on first write), so an organisation created
after 0321 legitimately has no row — and its ``organisations`` values are then
the only copy. ``upgrade()`` inserts a cost-state row for exactly those
organisations from their current column values BEFORE any column is dropped,
so a rewind restores them from the surviving store like every other
organisation (and keeps their pre-cutover values).

SQLite parity (0322 pattern): SQLite has no ``ADD COLUMN IF NOT EXISTS`` /
``ADD CONSTRAINT`` / ``DROP COLUMN IF EXISTS``, so both directions branch to
Alembic batch mode, which recreates the table from the model shape.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

# INFO-only observability routes to `alembic upgrade` output via a child of
# `alembic.runtime` (the 0192/0215 precedent).
_log = logging.getLogger("alembic.runtime.migration")

revision: str = "0324_drop_organisations_cost_columns"
down_revision: str | None = "0323_pipeline_snapshots_substantive_version_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organisations"
_COST_STATE_TABLE = "organisation_cost_state"

#: The three retired per-org cost columns, in the order 0145 added them.
_COST_COLUMNS: tuple[str, ...] = (
    "max_run_cost_cents",
    "spend_ceiling_cents",
    "org_cumulative_spend_cents",
)

_CHECK_NAME = "ck_organisations_cum_spend"
_CHECK_PREDICATE = "org_cumulative_spend_cents >= 0"

#: Literal DDL only — no f-string/format-built SQL (the migration-fstring-sql
#: semgrep rule), and every statement is table-qualified so a same-named
#: constraint on another table can never make an existence gate silently skip.
_DROP_CHECK_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname = 'ck_organisations_cum_spend' "
    "AND conrelid = 'public.organisations'::regclass) "
    "THEN ALTER TABLE public.organisations DROP CONSTRAINT ck_organisations_cum_spend; "
    "END IF; END $$;"
)

_ADD_CHECK_NOT_VALID_SQL = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname = 'ck_organisations_cum_spend' "
    "AND conrelid = 'public.organisations'::regclass) "
    "THEN ALTER TABLE public.organisations ADD CONSTRAINT "
    "ck_organisations_cum_spend CHECK (org_cumulative_spend_cents >= 0) NOT VALID; "
    "END IF; END $$;"
)

_VALIDATE_CHECK_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname = 'ck_organisations_cum_spend' "
    "AND conrelid = 'public.organisations'::regclass "
    "AND NOT convalidated) "
    "THEN ALTER TABLE public.organisations VALIDATE CONSTRAINT ck_organisations_cum_spend; "
    "END IF; END $$;"
)

#: The correlation that defines "this organisation HAS a cost-state row".
#: ONE constant, referenced by the pre-drop backfill's anti-join, the Postgres
#: restore and the SQLite restore, so the dialect branches cannot drift apart.
#: The Postgres branch is covered by the testcontainers integration test; the
#: SQLite branch runs inside Alembic batch mode and can only be exercised as
#: raw SQL, so a branch that is free to re-derive its own predicate is exactly
#: how the two came to disagree (the SQLite one aborted on a no-row org).
_COST_STATE_MATCH_PREDICATE = "s.org_id = o.id"

#: Pre-drop backfill — give every organisation WITHOUT a cost-state row one
#: carrying its current ``organisations`` values, BEFORE those columns are
#: dropped, so the drop is lossless for that class. 0321 backfills only the
#: organisations that existed when it ran and the application creates the row
#: lazily, so an organisation created after 0321 has none. A plain
#: INSERT..SELECT on two rows-per-org tables: no ``ALTER``, so it takes no
#: ACCESS EXCLUSIVE lock on ``organisations`` and cannot invert the lock order
#: against the DDL below. The migration connection is the DDL/admin role
#: (env.py selects ``DATABASE_ADMIN_URL``, provisioned BYPASSRLS), so the FORCE
#: RLS policy on ``organisation_cost_state`` does not filter this cross-org
#: insert.
_BACKFILL_MISSING_SQL = (
    "INSERT INTO organisation_cost_state "  # noqa: S608  # nosec B608 - module constants only
    "(org_id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents) "
    "SELECT o.id, o.max_run_cost_cents, o.spend_ceiling_cents, o.org_cumulative_spend_cents "
    "FROM organisations o "
    "WHERE NOT EXISTS (SELECT 1 FROM organisation_cost_state s WHERE " + _COST_STATE_MATCH_PREDICATE + ")"
)

#: Postgres downgrade restore. ``UPDATE ... FROM`` touches only organisations
#: that HAVE a cost-state row (the upgrade's backfill guarantees every
#: organisation that predates the drop does), so the rest keep the column
#: defaults. See the module docstring for the precedence this encodes.
_RESTORE_SQL = (
    "UPDATE organisations o SET "  # noqa: S608  # nosec B608 - module constants only
    "max_run_cost_cents = s.max_run_cost_cents, "
    "spend_ceiling_cents = s.spend_ceiling_cents, "
    "org_cumulative_spend_cents = s.org_cumulative_spend_cents "
    "FROM organisation_cost_state s "
    "WHERE " + _COST_STATE_MATCH_PREDICATE
)

#: SQLite downgrade restore — the SAME predicate, expressed as the
#: ``WHERE EXISTS`` anti-join SQLite needs. The scalar subqueries previously
#: used here yielded NULL for an organisation with no cost-state row, and the
#: NOT NULL cumulative column rejected the whole UPDATE, aborting the
#: downgrade; ``WHERE EXISTS`` restores only matching rows and leaves the rest
#: on the column default, exactly like the Postgres ``UPDATE ... FROM``.
#: ``COALESCE`` on the cumulative total keeps the statement from ever writing
#: NULL into a NOT NULL column even if the source row is malformed.
_RESTORE_SQLITE_SQL = (
    "UPDATE organisations AS o SET "  # noqa: S608  # nosec B608 - module constants only
    "max_run_cost_cents = (SELECT s.max_run_cost_cents FROM organisation_cost_state s "
    "WHERE " + _COST_STATE_MATCH_PREDICATE + "), "
    "spend_ceiling_cents = (SELECT s.spend_ceiling_cents FROM organisation_cost_state s "
    "WHERE " + _COST_STATE_MATCH_PREDICATE + "), "
    "org_cumulative_spend_cents = COALESCE("
    "(SELECT s.org_cumulative_spend_cents FROM organisation_cost_state s "
    "WHERE " + _COST_STATE_MATCH_PREDICATE + "), 0) "
    "WHERE EXISTS (SELECT 1 FROM organisation_cost_state s WHERE " + _COST_STATE_MATCH_PREDICATE + ")"
)

_DDL_LOCK_TIMEOUT = "30s"
_DDL_RETRIES = 3
_DDL_RETRY_SLEEP_SECONDS = 1.0


def _is_postgres(bind: sa.Connection) -> bool:
    return bool(bind.dialect.name == "postgresql")


def _ddl_with_retry(bind: Connection, sql: str, *, description: str) -> None:
    """One ``ALTER TABLE`` — lock_timeout + bounded savepoint retry.

    The 0129/0215 precedent, generalised because the downgrade needs bounded
    DDL too (its ``ADD COLUMN`` / ``ADD CONSTRAINT`` statements take the same
    ``ACCESS EXCLUSIVE`` lock). ``SET LOCAL lock_timeout`` bounds how long the
    statement waits for the lock; a refused attempt rolls back ONLY its own
    savepoint (re-arming the outer transaction's schema state, which is what
    makes the retry of a partially-applied DDL sequence safe) and retries. The
    FINAL failure propagates — never swallowed — and release.sh's bounded
    migrator re-runs the whole chain.

    ``sql`` is always a module constant or a concatenation of module constants;
    no caller data is ever interpolated (``SET LOCAL`` cannot take a bound
    parameter under every driver, hence the constant literal).
    """
    lock_sql = "SET LOCAL lock_timeout = '" + _DDL_LOCK_TIMEOUT + "'"
    for attempt in range(1, _DDL_RETRIES + 1):
        try:
            with bind.begin_nested():
                bind.execute(sa.text(lock_sql))
                bind.execute(sa.text(sql))
            return
        except OperationalError:
            if attempt == _DDL_RETRIES:
                raise
            _log.warning(
                "FAR-1902 organisations cost-columns: %s refused (attempt %d/%d) — retrying",
                description,
                attempt,
                _DDL_RETRIES,
            )
            time.sleep(_DDL_RETRY_SLEEP_SECONDS)


def _cost_state_table_exists(bind: Connection) -> bool:
    return (
        bind.execute(sa.text("SELECT to_regclass('public.organisation_cost_state')")).scalar_one_or_none() is not None
    )


def _sqlite_check_constraints(bind: Connection, table: str) -> set[str]:
    return {str(check.get("name")) for check in sa.inspect(bind).get_check_constraints(table)}


def _sqlite_table_exists(bind: Connection, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def _upgrade_sqlite(bind: Connection) -> None:
    # Backfill BEFORE the batch recreate drops the columns: the recreate is
    # what destroys their values, so a no-row organisation must be captured
    # first or the SQLite drop is as lossy as the Postgres one would be.
    if _sqlite_table_exists(bind, _COST_STATE_TABLE):
        bind.execute(sa.text(_BACKFILL_MISSING_SQL))
    constraints = _sqlite_check_constraints(bind, _TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        if _CHECK_NAME in constraints:
            batch.drop_constraint(_CHECK_NAME, type_="check")
        for column in _COST_COLUMNS:
            batch.drop_column(column)


def _downgrade_sqlite(bind: Connection) -> None:
    has_cost_state = _sqlite_table_exists(bind, _COST_STATE_TABLE)
    constraints = _sqlite_check_constraints(bind, _TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("max_run_cost_cents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("spend_ceiling_cents", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("org_cumulative_spend_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
        if _CHECK_NAME not in constraints:
            batch.create_check_constraint(_CHECK_NAME, _CHECK_PREDICATE)
    if has_cost_state:
        bind.execute(sa.text(_RESTORE_SQLITE_SQL))


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        _upgrade_sqlite(bind)
        return

    bind.execute(sa.text("SET search_path TO public"))

    # Lossless drop: capture the values of any organisation that has no
    # cost-state row BEFORE anything is dropped. 0321 backfilled only the
    # organisations existing at its own run and the application creates the row
    # lazily, so an organisation created since then has none — and its
    # ``organisations`` values are the only copy this migration could destroy.
    backfilled = bind.execute(sa.text(_BACKFILL_MISSING_SQL)).rowcount or 0
    _log.info(
        "FAR-1902: backfilled %d organisation(s) with no cost-state row before the drop",
        backfilled,
    )

    # Drop the CHECK FIRST so the intent is explicit in the deploy log; the
    # column drop would otherwise cascade it silently (a CHECK that references
    # only the dropped column is removed with it).
    _ddl_with_retry(bind, _DROP_CHECK_SQL, description="DROP CONSTRAINT organisations." + _CHECK_NAME)

    # One bounded statement per column: a single multi-column ALTER would make a
    # lock refusal retry every column together, and per-statement retries keep
    # each attempt's savepoint narrow (the 0215 shape).
    for column in _COST_COLUMNS:
        _ddl_with_retry(
            bind,
            "ALTER TABLE public.organisations DROP COLUMN IF EXISTS " + column,
            description="DROP COLUMN organisations." + column,
        )

    _log.info(
        "FAR-1902: dropped organisations cost columns %s and CHECK %s "
        "(lock_timeout=%s, per-attempt savepoint retry on every DDL statement)",
        ", ".join(_COST_COLUMNS),
        _CHECK_NAME,
        _DDL_LOCK_TIMEOUT,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        _downgrade_sqlite(bind)
        return

    bind.execute(sa.text("SET search_path TO public"))

    # The restore source is guaranteed by the chain: 0321 (this migration's
    # ancestor) creates organisation_cost_state and only ITS downgrade drops it,
    # which runs strictly after this one. An absent table therefore means the
    # database is in a state this downgrade cannot restore values from — abort
    # loudly rather than re-add three silently empty columns.
    if not _cost_state_table_exists(bind):
        raise RuntimeError(
            "FAR-1902 downgrade cannot restore organisations cost columns: "
            "'public.organisation_cost_state' does not exist. The table is created by 0321 and only dropped by "
            "0321's own downgrade (which runs after this one), so its absence means the schema is not on the "
            "0321-0323 lineage this migration's downgrade expects. Remediate by hand, then re-run."
        )

    _ddl_with_retry(
        bind,
        "ALTER TABLE public.organisations ADD COLUMN IF NOT EXISTS max_run_cost_cents INTEGER",
        description="ADD COLUMN organisations.max_run_cost_cents",
    )
    _ddl_with_retry(
        bind,
        "ALTER TABLE public.organisations ADD COLUMN IF NOT EXISTS spend_ceiling_cents INTEGER",
        description="ADD COLUMN organisations.spend_ceiling_cents",
    )
    # NOT NULL with a constant default: metadata-only on PostgreSQL 11+, so this
    # does not rewrite the table.
    _ddl_with_retry(
        bind,
        "ALTER TABLE public.organisations ADD COLUMN IF NOT EXISTS "
        "org_cumulative_spend_cents INTEGER NOT NULL DEFAULT 0",
        description="ADD COLUMN organisations.org_cumulative_spend_cents",
    )

    restored = bind.execute(sa.text(_RESTORE_SQL)).rowcount or 0
    _log.info(
        "FAR-1902 downgrade: restored %d organisation(s)' cost columns from organisation_cost_state",
        restored,
    )

    # Re-add the CHECK NOT VALID then VALIDATE (0151/0322 pattern): the ADD takes
    # only a brief lock and never aborts on historical rows; the VALIDATE is the
    # online row scan. The restored values come from a column that already
    # carries the same ``>= 0`` guarantee, so the validation passes.
    _ddl_with_retry(bind, _ADD_CHECK_NOT_VALID_SQL, description="ADD CONSTRAINT organisations." + _CHECK_NAME)
    _ddl_with_retry(bind, _VALIDATE_CHECK_SQL, description="VALIDATE CONSTRAINT organisations." + _CHECK_NAME)
