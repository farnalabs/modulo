"""Organisation cost state table + backfill + RLS (FAR-1823 Land 2).

Creates ``organisation_cost_state`` and backfills one row per existing
``organisations`` row (copying ``max_run_cost_cents`` / ``spend_ceiling_cents`` /
``org_cumulative_spend_cents``).

This is the EXPAND half of an expand/contract cutover. The three source
``organisations`` columns are deliberately LEFT IN PLACE: deployments apply
migrations once BEFORE machines roll (``deploy/fly/release.sh``, rolling
strategy), so dropping them in the same release that stops reading them would
break any old-code instance still serving during the rollout window. Code no
longer reads or writes them; a follow-up migration drops the columns and the
``ck_organisations_cum_spend`` CHECK once this release is live, with no rollout
window at all.

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

No destructive DDL: this migration only creates the new table, so it takes no
``ACCESS EXCLUSIVE`` lock on ``organisations`` and needs none of the bounded
``lock_timeout``/savepoint-retry machinery (0129/0215). The follow-up drop
migration owns that pattern for its own ``ALTER TABLE organisations`` statements.

Reversible: the downgrade drops the RLS policy, disables RLS and drops the new
table. Both directions leave the three ``organisations`` columns untouched, so a
rollback loses no pre-existing data.

Revision ID: 0317_organisation_cost_state
Revises: 0316_policy_gate_evaluation_mode
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0317_organisation_cost_state"
down_revision: str | None = "0316_policy_gate_evaluation_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organisation_cost_state"
_CUM_SPEND_CHECK = "ck_organisation_cost_state_cum_spend"

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

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
    # Genuine inverse of the (now purely additive) upgrade: tear down the RLS
    # policy and drop the new table. The three ``organisations`` columns are
    # never touched in either direction.
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute("SET search_path TO public")
        op.execute(_DROP_POLICY_STATEMENT)
        op.execute(_DISABLE_RLS_STATEMENT)

    op.drop_table(_TABLE)
