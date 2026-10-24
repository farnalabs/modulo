"""Create run_attempts — durable per-attempt execution history (FAR-2078 slice 1).

Revision ID: 0329_run_attempts
Revises: 0328_evidence_index_aggregate_indexes
Create Date: 2026-10-23

Renumbered from ``0326_run_attempts`` and re-pointed from
``0325_run_seam_coverage`` when main advanced to
``0327_runs_execution_attempt_count`` and the merge queue reported a
multi-head collision with PR #2165 (``0328_eval_verdict_contract``). Re-pointed
again from ``0327_runs_execution_attempt_count`` to
``0328_evidence_index_aggregate_indexes`` when main advanced to 0328, which
would otherwise branch the graph into two heads (0328 and this revision). The
migration is unchanged; only its position in the chain moved.

A new org-scoped child table of ``runs`` holding one row per execution attempt
of a run. Before this table only the FINAL attempt's triage survived, so an
intermediate attempt that stalled, hit the node deadline, exhausted the guard
cap or lost its worker left no durable record — diagnosing the 2026-10-22
Branch Fixer timeline took ~1h from audit events and ``dispatched_at``
rewrites.

This migration is the storage layer ONLY; the executor instrumentation that
writes rows and the API/UI that reads them ship separately. It creates the
table with NO backfill: per-attempt history that was never recorded cannot be
reconstructed, so the table starts empty and every future attempt gets a row.

Column contract (see ``modulo.db.models.run_attempt`` for the full prose):

* ``attempt_index`` — 0-based, per run, contiguous and monotonic; the first
  attempt of a run is 0. ``uq_run_attempts_run_attempt_index`` makes
  ``(run_id, attempt_index)`` unique and its btree doubles as the index for
  "all attempts for one run" (leftmost prefix ``run_id`` + the sort column), so
  no second index on the pair is created (that would be redundant).
* ``terminal_reason`` — CLOSED vocabulary (deadline / stall / guard_cap /
  worker_lost / provider_error / completed / reset); NULL means the attempt is
  still OPEN and ``ended_at`` is NULL too.
* ``started_at`` / ``ended_at`` are ``timestamptz``; ``created_at`` /
  ``updated_at`` follow the house ``TimestampMixin``.

Retention: ``run_id`` is ``FOREIGN KEY ... ON DELETE CASCADE`` to ``runs``, so
every run-deletion path (the age-based retention purge and org deletion)
removes the run's attempts in the same statement. ``organisation_id`` is also
CASCADE (the tenant anchor shared by the other run child tables). No separate
sweep is needed and the rows cannot outlive their run.

RLS: an org-scoped table, so it gets the mandatory four-step pattern (owner
``modulo_migrate``, ENABLE + FORCE ROW LEVEL SECURITY, the canonical
``rls_org_isolation`` policy) plus DML grants to ``modulo_app`` /
``modulo_system`` when those roles exist. Non-Postgres backends get the table
and indexes only (SQLite relies on app-level tenant filtering; MariaDB is
deprecated).

Lock-safety: ``CREATE TABLE`` on a brand-new relation takes no lock on ``runs``
or ``organisations`` beyond the brief reference locks, so none of the
bounded-``lock_timeout`` machinery the hot-table rewrites need applies here.

Additively reversible: the downgrade drops the policy, disables RLS, drops the
org index and the table. No existing data is touched in either direction.

Authoring note (semgrep ``migration-fstring-sql``): this revision is above the
rule's 0247 cutoff, so every interpolated value is a module-level constant
assembled with plain concatenation — no f-string reaches ``op.execute``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0329_run_attempts"
down_revision: str | None = "0328_evidence_index_aggregate_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "run_attempts"
_INDEX_ORG = "ix_run_attempts_organisation_id"

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

# Module-level tuple of the org-scoped tables this migration secures, so the
# architecture test (tests/unit/db/test_rls_coverage.py) sees the coverage
# without parsing the DDL below.
_ORG_SCOPED_TABLES: tuple[str, ...] = (_TABLE,)

# Frozen copy of the ORM vocabulary in modulo.db.models.run_attempt. A shipped
# migration must not depend on live code; the migration test asserts the two
# are identical, so a vocabulary change without a widening migration fails CI.
_TERMINAL_REASON_SQL = (
    "terminal_reason IN ('deadline', 'stall', 'guard_cap', 'worker_lost', 'provider_error', 'completed', 'reset')"
)

_ORG_ISOLATION_POLICY = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

# Plain string concatenation (never an f-string) keeps the migration-fstring-sql
# rule satisfied while interpolating only module-level constants.
_ALTER_TABLE = 'ALTER TABLE "' + _TABLE + '"'
_ENABLE_RLS_STATEMENTS: tuple[str, ...] = (
    _ALTER_TABLE + " ENABLE ROW LEVEL SECURITY",
    _ALTER_TABLE + " FORCE ROW LEVEL SECURITY",
)
_CREATE_POLICY_STATEMENT = 'CREATE POLICY rls_org_isolation ON "' + _TABLE + '" USING (' + _ORG_ISOLATION_POLICY + ")"
_DROP_POLICY_STATEMENT = 'DROP POLICY IF EXISTS rls_org_isolation ON "' + _TABLE + '"'
_DISABLE_RLS_STATEMENT = _ALTER_TABLE + " DISABLE ROW LEVEL SECURITY"

_GRANT_CREATE_SCHEMA = "GRANT CREATE ON SCHEMA public TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_ORGS = "GRANT REFERENCES ON TABLE public.organisations TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_RUNS = "GRANT REFERENCES ON TABLE public.runs TO " + _MIGRATE_ROLE
_SET_ROLE_MIGRATE = "SET ROLE " + _MIGRATE_ROLE


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl="public." + table
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            table + " owner is " + repr(owner) + ", expected '" + _MIGRATE_ROLE + "' "
            "(the app role must NOT own it — owner bypasses RLS)"
        )


def _create_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_reason", sa.String(length=30), nullable=True),
        sa.Column("model_used", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # The natural key of the attempt sequence. Its backing index also serves
        # "all attempts for one run" — see the module docstring.
        sa.UniqueConstraint("run_id", "attempt_index", name="uq_run_attempts_run_attempt_index"),
        sa.CheckConstraint("attempt_index >= 0", name="ck_run_attempts_attempt_index_non_negative"),
        sa.CheckConstraint(_TERMINAL_REASON_SQL, name="ck_run_attempts_terminal_reason"),
        # No ``ended_at >= started_at`` CHECK on purpose: the two instants may
        # come from different clocks, and a diagnostic row must not be rejected
        # because they disagree by a second.
    )


def _create_indexes() -> None:
    # Only the tenant anchor: the (run_id, attempt_index) unique constraint
    # already indexes the run's attempt sequence.
    op.create_index(_INDEX_ORG, _TABLE, ["organisation_id"])


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    # migrate_role can only be True when pg (resolved under the pg guard).
    migrate_role = False
    app_role = False
    system_role = False
    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        system_role = _role_exists(bind, _SYSTEM_ROLE)
        if migrate_role:
            op.execute(_GRANT_CREATE_SCHEMA)
            op.execute(_GRANT_REFERENCES_ORGS)
            op.execute(_GRANT_REFERENCES_RUNS)

    if migrate_role:
        op.execute(_SET_ROLE_MIGRATE)

    _create_table()

    if migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    _create_indexes()

    if pg:
        for statement in _ENABLE_RLS_STATEMENTS:
            op.execute(statement)
        op.execute(_CREATE_POLICY_STATEMENT)
        if app_role:
            op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON " + _TABLE + " TO " + _APP_ROLE)
        if system_role:
            op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON " + _TABLE + " TO " + _SYSTEM_ROLE)


def downgrade() -> None:
    bind = op.get_bind()

    if _is_postgres(bind):
        op.execute("SET search_path TO public")
        op.execute(_DROP_POLICY_STATEMENT)
        op.execute(_DISABLE_RLS_STATEMENT)

    op.drop_index(_INDEX_ORG, table_name=_TABLE)
    op.drop_table(_TABLE)
