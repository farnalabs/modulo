"""org_mint_budget_usage — atomic per-org agent-mint budget tally (FAR-795).

Revision ID: 0233_org_mint_budget_usage
Revises: 0232_journey_dismissal
Create Date: 2026-09-14

What this migration does:

* ``org_mint_budget_usage`` — one row per ``(organisation_id, window_start)``:
  the usage tally of agent-sourced work-item minting (FAR-795) within a
  rolling budget window. The tally is written by a SINGLE upsert
  (``INSERT ... ON CONFLICT DO UPDATE ... WHERE used + :n <= :limit ...``)
  from :mod:`modulo.core.runtime_config.mint_budget` — never read-modify-write
  in Python — so a window's row is the source of truth for how much of the
  org's per-window mint budget is consumed. A new ``window_start`` is a new
  row, so rollover resets with zero code.
* ``ix_org_mint_budget_usage_window_start`` — the periodic cleanup sweep
  deletes whole windows older than the retention horizon
  (``DELETE ... WHERE window_start < :cutoff``); the leading-PK composite
  index does not serve that predicate because ``organisation_id`` leads it.
* Composite PRIMARY KEY ``(organisation_id, window_start)`` — also the
  ON CONFLICT target of the budget statement; no separate unique constraint.
* RLS — ENABLE + FORCE ROW LEVEL SECURITY + ``rls_org_isolation`` on the
  table (owner ``modulo_migrate``), same ceremony as the other org-scoped
  tables (the 0204 precedent): the app role must never read or spend
  another org's budget tally. Grants to ``modulo_app`` (mint paths) and
  ``modulo_system`` (worker/session processes that BYPASSRLS still must not
  be missing table privileges — the 0204 qa-F14 lesson).

No data legs: the tally starts empty; historical minting was ungoverned and
must NOT be backfilled as usage.

Downgrade: drops the table (and its RLS objects). In-flight windows lose
their tally downgrade-side-up — a budget cap becoming temporarily more
permissive, never less.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0233_org_mint_budget_usage"
down_revision: str | None = "0232_journey_dismissal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "org_mint_budget_usage"

# Module-level tuple of org-scoped tables secured with RLS by this migration,
# so the architecture test (test_rls_coverage) can detect the coverage.
# (Style-2 collection — 0204 precedent.)
_ORG_SCOPED_TABLES = (_TABLE,)


def _is_postgres(bind: sa.Connection) -> bool:
    return str(bind.dialect.name).startswith("postgres")


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl=f"public.{table}"
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"{table} owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own it — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        system_role = _role_exists(bind, _SYSTEM_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
    else:
        # SQLite (unit-test schemas): tables are created directly by the
        # migration below (the budget module has no ORM model to fall back
        # on); only the role/RLS ceremony is POSTgres-only.
        migrate_role = False
        app_role = False
        system_role = False

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        _TABLE,
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organisation_id", "window_start", name="pk_org_mint_budget_usage"),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    op.create_index("ix_org_mint_budget_usage_window_start", _TABLE, ["window_start"])
    op.create_index("ix_org_mint_budget_usage_organisation_id", _TABLE, ["organisation_id"])

    if pg:
        op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")
        # qa F14 (0204 precedent): EXPLICIT system-role privileges — the
        # worker/session processes run as modulo_system; BYPASSRLS skips row
        # security but NOT table privileges, and a missing grant would
        # fail-open every budget consume (the guard allows on any error).
        if system_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_SYSTEM_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_TABLE}")
        op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_org_mint_budget_usage_organisation_id", table_name=_TABLE)
    op.drop_index("ix_org_mint_budget_usage_window_start", table_name=_TABLE)

    op.drop_table(_TABLE)
