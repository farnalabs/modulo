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

The downgrade RESTORES THE DATA, not just the columns. It re-adds the three
columns and copies each value back from ``organisation_cost_state`` — the exact
inverse of 0321's backfill — so a rewind loses none of the values that lived on
``organisations`` before this migration. That cost-state table is still present
at this revision (0321 only drops it in its OWN downgrade, which runs after
this one), so the restore source is guaranteed. An organisation with no
cost-state row — possible for an org created after 0321 that never set a ceiling
and never accrued spend — keeps the column defaults (NULL ceilings, ``0``
consumed), which is exactly the pre-cutover semantic for such an org.

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

#: Downgrade restore — the exact inverse of 0321's backfill. ``UPDATE ... FROM``
#: touches only organisations that HAVE a cost-state row; the rest keep the
#: column defaults, which is their pre-cutover semantic (no ceilings, 0 spent).
_RESTORE_SQL = (
    "UPDATE organisations o SET "
    "max_run_cost_cents = s.max_run_cost_cents, "
    "spend_ceiling_cents = s.spend_ceiling_cents, "
    "org_cumulative_spend_cents = s.org_cumulative_spend_cents "
    "FROM organisation_cost_state s "
    "WHERE s.org_id = o.id"
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
        bind.execute(
            sa.text(
                "UPDATE organisations SET "
                "max_run_cost_cents = (SELECT s.max_run_cost_cents FROM organisation_cost_state s "
                "WHERE s.org_id = organisations.id), "
                "spend_ceiling_cents = (SELECT s.spend_ceiling_cents FROM organisation_cost_state s "
                "WHERE s.org_id = organisations.id), "
                "org_cumulative_spend_cents = (SELECT s.org_cumulative_spend_cents FROM organisation_cost_state s "
                "WHERE s.org_id = organisations.id)"
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        _upgrade_sqlite(bind)
        return

    bind.execute(sa.text("SET search_path TO public"))

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
