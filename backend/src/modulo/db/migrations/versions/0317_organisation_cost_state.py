"""Organisation cost state table + cutover (FAR-1823 Land 2).

Creates ``organisation_cost_state``, backfills one row per existing
``organisations`` row (copying ``max_run_cost_cents`` / ``spend_ceiling_cents`` /
``org_cumulative_spend_cents``), then DROPS those three ``organisations`` columns
and the ``ck_organisations_cum_spend`` CHECK that referenced the cumulative one.

Tenancy (RLS): the table is keyed ``org_id`` (NOT ``organisation_id``), so it is
deliberately NOT covered by the generic ``organisation_id`` RLS sweep — its
policy is written explicitly against ``org_id``. It STILL enables ``ROW LEVEL
SECURITY`` (and FORCE), because on Postgres the ORM tenant filter is a no-op
(``db/rls.py`` returns early for ``postgres``), so RLS is the only DB-level
backstop. The org-scoped nature is real: the row is mutable, carries
``ON DELETE CASCADE`` and MUST NOT survive its organisation — unlike
``system_audit_events``, whose ``org_id`` naming note this table's earlier
docstring referenced. That analogy was INAPT: ``system_audit_events`` is
append-only, has no FK and must OUTLIVE org deletion; ``organisation_cost_state``
is the exact opposite. Access is always by an explicit organisation id, and every
production path (the admin cost routes, the finalize gate+accrual, the executor
pre-dispatch gate) runs with ``set_rls_org`` bound — see FAR-1833 P4.

DDL waits (FAR-1833 P2-3): the three ``organisations`` column drops + the CHECK
drop need ``ACCESS EXCLUSIVE`` on ``organisations``; an unbounded wait would
queue EVERY subsequent query on that table behind it. Each destructive statement
runs with a transaction-scoped ``lock_timeout`` (30 s) inside a per-attempt
SAVEPOINT with bounded retries — the 0129/0215 precedent — so a contended drop
FAILS FAST (release.sh retries ``alembic upgrade heads`` 3x) instead of queueing
unboundedly.

Additively reversible: the downgrade re-adds the three ``organisations`` columns,
backfills them from the cost-state row, re-adds the CHECK, then drops the table —
so a rollback does not lose data.

Revision ID: 0317_organisation_cost_state
Revises: 0316_policy_gate_evaluation_mode
Create Date: 2026-10-16
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision: str = "0317_organisation_cost_state"
down_revision: str | None = "0316_policy_gate_evaluation_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organisation_cost_state"
_CUM_SPEND_CHECK = "ck_organisation_cost_state_cum_spend"
_ORG_CUM_SPEND_CHECK = "ck_organisations_cum_spend"

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

# DDL-wait bound for the destructive ``organisations`` statements (0129/0215
# precedent). ``SET LOCAL`` is transaction-scoped and reverted with its savepoint.
_DDL_LOCK_TIMEOUT = "30s"
_DDL_RETRIES = 3
_DDL_RETRY_SLEEP_SECONDS = 1.0

# The three ``organisations`` cost columns this migration drops (dropped in this
# order — matches the former op.drop_column sequence for a stable diff).
_ORG_COST_COLUMNS: tuple[str, ...] = (
    "max_run_cost_cents",
    "spend_ceiling_cents",
    "org_cumulative_spend_cents",
)

# Module-level tuple of the org-scoped tables this migration secures, so the
# architecture test (tests/unit/db/test_rls_coverage.py) sees the coverage
# without parsing the DDL below.
_ORG_SCOPED_TABLES: tuple[str, ...] = (_TABLE,)

# The strict, fail-closed scope, the ``org_id`` twin of the
# ``rls_org_isolation`` predicate the sibling migrations use: a null/empty
# ``app.organisation_id`` yields NULL which matches no row.
_ORG_SCOPE = "(org_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"

# Plain string concatenation (never an f-string to op.execute) keeps the
# migration-fstring-sql rule satisfied while interpolating only module constants.
_ENABLE_RLS_STATEMENTS: tuple[str, ...] = (
    'ALTER TABLE "' + _TABLE + '" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "' + _TABLE + '" FORCE ROW LEVEL SECURITY',
)
_CREATE_POLICY_STATEMENT = 'CREATE POLICY rls_org_isolation ON "' + _TABLE + '" USING (' + _ORG_SCOPE + ")"
_DROP_POLICY_STATEMENT = 'DROP POLICY IF EXISTS rls_org_isolation ON "' + _TABLE + '"'
_DISABLE_RLS_STATEMENT = 'ALTER TABLE "' + _TABLE + '" DISABLE ROW LEVEL SECURITY'

# Backfill: one row per existing organisation, copying the three current values.
# The table is created empty by this same upgrade, so no org can already have a
# row and the plain INSERT cannot conflict. Static literal (no f-string / no
# interpolation) — the migration-fstring-sql rule for revisions above 0247.
_BACKFILL_SQL = (
    "INSERT INTO organisation_cost_state "
    "(org_id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents) "
    "SELECT id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents "
    "FROM organisations"
)

# Reverse backfill (downgrade): copy the values back onto the re-added
# ``organisations`` columns. An org with no cost-state row keeps the column
# defaults (ceiling NULL, cumulative 0). Static literal.
_RESTORE_ORG_COLUMNS_SQL = (
    "UPDATE organisations o "
    "SET max_run_cost_cents = s.max_run_cost_cents, "
    "    spend_ceiling_cents = s.spend_ceiling_cents, "
    "    org_cumulative_spend_cents = s.org_cumulative_spend_cents "
    "FROM organisation_cost_state s "
    "WHERE s.org_id = o.id"
)


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


def _drop_ddl_with_retry(bind: sa.Connection, ddl: str, description: str) -> None:
    """One destructive DDL drop — ``lock_timeout`` + bounded savepoint retry.

    Mirrors 0215's ``_drop_with_retry`` (the 0129 stale-lock precedent): an
    ``ACCESS EXCLUSIVE`` request on ``organisations`` with no timeout can wedge
    the whole upward migration chain behind it. A refused attempt rolls back ONLY
    its savepoint (re-arming the outer transaction's schema state) and retries;
    the FINAL failure propagates — release.sh's bounded migrator re-runs the
    chain 3x.
    """
    lock_sql = "SET LOCAL lock_timeout = '" + _DDL_LOCK_TIMEOUT + "'"
    for attempt in range(1, _DDL_RETRIES + 1):
        try:
            with bind.begin_nested():
                bind.execute(sa.text(lock_sql))
                bind.execute(sa.text(ddl))
            return
        except OperationalError:
            if attempt == _DDL_RETRIES:
                raise
            time.sleep(_DDL_RETRY_SLEEP_SECONDS)


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    migrate_role = False
    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        if migrate_role:
            op.execute("GRANT CREATE ON SCHEMA public TO " + _MIGRATE_ROLE)
            op.execute("GRANT REFERENCES ON TABLE public.organisations TO " + _MIGRATE_ROLE)
            # The backfill below reads ``organisations`` while running as
            # modulo_migrate; REFERENCES alone does not grant SELECT.
            op.execute("GRANT SELECT ON TABLE public.organisations TO " + _MIGRATE_ROLE)

    if migrate_role:
        op.execute("SET ROLE " + _MIGRATE_ROLE)

    op.create_table(
        _TABLE,
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("max_run_cost_cents", sa.Integer(), nullable=True),
        sa.Column("spend_ceiling_cents", sa.Integer(), nullable=True),
        sa.Column("org_cumulative_spend_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["org_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
        sa.CheckConstraint("org_cumulative_spend_cents >= 0", name=_CUM_SPEND_CHECK),
    )
    op.execute(_BACKFILL_SQL)

    if migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    # Cutover: drop the CHECK that references the cumulative column first, then
    # the three columns themselves. On Postgres each destructive statement is
    # bounded by ``lock_timeout`` + a per-attempt savepoint retry (FAR-1833
    # P2-3); the non-Postgres path keeps the plain Alembic ops.
    if pg:
        _drop_ddl_with_retry(
            bind,
            "ALTER TABLE organisations DROP CONSTRAINT " + _ORG_CUM_SPEND_CHECK,
            "DROP CONSTRAINT organisations." + _ORG_CUM_SPEND_CHECK,
        )
        for column in _ORG_COST_COLUMNS:
            _drop_ddl_with_retry(
                bind,
                "ALTER TABLE organisations DROP COLUMN " + column,
                "DROP COLUMN organisations." + column,
            )
    else:
        op.drop_constraint(_ORG_CUM_SPEND_CHECK, "organisations", type_="check")
        for column in _ORG_COST_COLUMNS:
            op.drop_column("organisations", column)

    # RLS: an explicit ``org_id`` policy (this table is outside the generic
    # ``organisation_id`` sweep). ENABLE + FORCE so the table owner is subject
    # too, and grant DML to the runtime roles.
    if pg:
        for statement in _ENABLE_RLS_STATEMENTS:
            op.execute(statement)
        op.execute(_CREATE_POLICY_STATEMENT)
        grant = "GRANT SELECT, INSERT, UPDATE, DELETE ON " + _TABLE + " TO "
        if _role_exists(bind, _APP_ROLE):
            op.execute(grant + _APP_ROLE)
        if _role_exists(bind, _SYSTEM_ROLE):
            op.execute(grant + _SYSTEM_ROLE)


def downgrade() -> None:
    # Genuine inverse: re-add the three columns (shape from 0145), backfill them
    # from the cost-state row, re-add the CHECK, then drop the new table.
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute("SET search_path TO public")
        op.execute(_DROP_POLICY_STATEMENT)
        op.execute(_DISABLE_RLS_STATEMENT)

    op.add_column("organisations", sa.Column("max_run_cost_cents", sa.Integer(), nullable=True))
    op.add_column("organisations", sa.Column("spend_ceiling_cents", sa.Integer(), nullable=True))
    op.add_column(
        "organisations",
        sa.Column("org_cumulative_spend_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.execute(_RESTORE_ORG_COLUMNS_SQL)
    op.create_check_constraint(_ORG_CUM_SPEND_CHECK, "organisations", "org_cumulative_spend_cents >= 0")
    op.drop_table(_TABLE)
