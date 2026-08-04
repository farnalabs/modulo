"""Multi-component cost tracking — cost_components table + run/ledger columns.

Revision ID: 0065_cost_components
Revises: 0064_merge_heads_0037
Create Date: 2026-08-04

THE REAL MIGRATION TREE (normative): the live head is ``0064_merge_heads_0037``
— a MERGE migration (revision id "0064_merge_heads_0037", down_revision
("0037_add_scheduled_reports_created_by", "0037_break_glass_enforcement")) that
merged the two live 0037_* heads. ``0063_merge_all_heads.py`` carries the
revision id "0036_merge_all_heads" but is NOT the head. This migration is a
NORMAL migration off the ACTUAL head, deployed via the EXISTING ``upgrade
heads`` (plural) form — NO pin.

STEP-0 HEAD ASSERTION (run BEFORE authoring, and again POST-authoring):
    $alembic_heads = uv run python -m alembic heads
    if ($LASTEXITCODE -ne 0) { throw 'uv/alembic command failed (not a head mismatch)' }
    $head_lines = @($alembic_heads | Where-Object { $_ -match '^\\S+ \\(head\\)' })
    if (($head_lines | Select-String '0064_merge_heads_0037').Count -eq 0) { throw 'wrong migration head' }
    if ($head_lines.Count -ne 1) { throw 'migration tree is not single-head' }
POST-authoring asserts ``0065_cost_components`` as the new sole head.

DDL MAINTENANCE-WINDOW FLAG: this migration runs in ONE transaction and holds
the ACCESS EXCLUSIVE lock for TWO blocking CREATE INDEX (``ix_runs_probe``,
``ix_runs_refusal``) + the ``op.drop_constraint`` + the NULLS NOT DISTINCT
re-create. Terminal finalizations stall for the build duration. Fine at
dogfood scale; budget a MAINTENANCE WINDOW at production scale.

ROLE WIRING: the migration connects via ``DATABASE_ADMIN_URL`` (env.py:52-55 —
the superuser/owner URL). ``modulo_migrate`` is a NOLOGIN role (bootstrap
role.py), so it cannot be CONNECTED to — the migration executes
``SET ROLE modulo_migrate`` BEFORE ``op.create_table("cost_components", ...)``
(a NOLOGIN role IS activatable via SET ROLE by a superuser), then ``RESET ROLE``
AFTER (the RLS-enable + policy + grant steps run as the migration's caller).
The post-create ownership assertion inside this migration verifies the created
table's owner is ``modulo_migrate``, not the app role — the owner-bypasses-RLS
precondition for ``cost_components`` RLS confinement.

The ``modulo_migrate`` role, ``CREATE POLICY``, and ``GRANT`` all require
superuser (or membership) on the ``DATABASE_ADMIN_URL`` — the whole 0065 run
depends on that URL actually being superuser- or owner-privileged (assumption
stated next to env.py:52-55).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_cost_components"
down_revision: str | None = "0064_merge_heads_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COST_COLUMN_CAP = "99999999.999999"
_RATE_COLUMN_CAP = "999999999999.999999"
_MIGRATE_ROLE = "modulo_migrate"

_STRICT_RLS = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _preflight_null_distinct_duplicates(bind: sa.Connection) -> None:
    """FIRST step of 0065, BEFORE any add_column AND before create_table.

    Fails LOUDLY when duplicate ``(organisation_id, NULL, run_date)`` rows
    exist (the NULLs-distinct pause+terminal double-record bug may already have
    produced them in production). Chosen over proceeding-with-duplicates so the
    NULLS NOT DISTINCT guarantee is real. Remediation:
    ``scripts/merge_org_daily_run_count_dupes.py``.
    """
    rows = bind.execute(
        sa.text(
            "SELECT organisation_id, run_date, COUNT(*) FROM org_daily_run_counts "
            "WHERE team_id IS NULL GROUP BY organisation_id, team_id, run_date HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if rows:
        detail = "; ".join(f"org={row[0]} date={row[1]} count={row[2]}" for row in rows)
        raise RuntimeError(
            "duplicate NULLS-distinct org_daily_run_counts rows present — merge first via "
            "backend/scripts/merge_org_daily_run_count_dupes.py: " + detail
        )


def _guard_org_daily_unique_constraint(bind: sa.Connection) -> None:
    """In-migration constraint-name guard — immediately before the drop.

    The authoritative copy of the ``uq_org_daily_run_counts_org_team_date``-
    present check lives IN THE MIGRATION (a name mismatch is caught as a named
    constraint-name error, not a generic drop error). The pre-flight gate's
    version is a CANARY copy.
    """
    conname = bind.execute(
        sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.org_daily_run_counts'::regclass "
            "AND conname = 'uq_org_daily_run_counts_org_team_date'"
        )
    ).scalar_one_or_none()
    if conname is None:
        raise RuntimeError(
            "expected unique constraint uq_org_daily_run_counts_org_team_date "
            "on public.org_daily_run_counts; found none (renamed/missing constraint?)"
        )


def _assert_cost_components_owner_is_migrate(bind: sa.Connection) -> None:
    """POST-CREATE ownership assertion INSIDE 0065 (after create_table, BEFORE RLS).

    The created table's owner must be ``modulo_migrate``, not the app role — if
    the migration ran as the app role the owner would bypass RLS and org
    confinement would silently vanish.
    """
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass('public.cost_components')")
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"cost_components owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own cost_components — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    # SET search_path pinned at the top (schema-qualified references resolve
    # against a pinned path, not the session's ambient search_path).
    if pg:
        op.execute("SET search_path TO public")

    # 0. NULLS NOT DISTINCT duplicate pre-flight — the FIRST step.
    if pg:
        _preflight_null_distinct_duplicates(bind)

    # 1. Drop the plain unique CONSTRAINT, recreate as a NULLS NOT DISTINCT
    #    unique index. Two concurrent first-of-day NULL-team terminals can no
    #    longer BOTH insert org rows.
    if pg:
        _guard_org_daily_unique_constraint(bind)
        op.drop_constraint(
            "uq_org_daily_run_counts_org_team_date",
            "org_daily_run_counts",
            type_="unique",
        )
        op.create_index(
            "uq_org_daily_run_counts",
            "org_daily_run_counts",
            ["organisation_id", "team_id", "run_date"],
            unique=True,
            postgresql_nulls_not_distinct=True,
        )

    # 2. cost_components table — created under SET ROLE modulo_migrate so the
    #    owner is the MIGRATE role (the app role must NOT own it: the owner
    #    bypasses RLS). ``formula`` is NULLABLE (NULL for self_reported).
    #    The MIGRATE-role deploy-wiring: modulo_migrate needs CREATE on the
    #    public schema + REFERENCES on organisations to create the table's
    #    org FK. bootstrap_role.py grants both on every boot, but the
    #    pre-alembic bootstrap runs on a fresh DB where organisations does not
    #    exist yet — so 0065 re-applies both grants itself (idempotent)
    #    right before SET ROLE.
    if pg:
        op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
        op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")
    op.create_table(
        "cost_components",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("rate_usd", sa.Numeric(18, 6), nullable=True),
        sa.Column("rate_fallback", sa.String(length=32), nullable=True),
        sa.Column("formula", sa.String(length=256), nullable=True),
        sa.Column("report_key", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.CheckConstraint("kind IN ('calculated', 'self_reported')", name="ck_cost_components_kind"),
    )
    if pg:
        op.create_index("ix_cost_components_organisation_id", "cost_components", ["organisation_id"])
        op.execute("RESET ROLE")
        _assert_cost_components_owner_is_migrate(bind)
        op.create_index(
            "ix_cost_components_org_enabled_sort", "cost_components", ["organisation_id", "enabled", "sort_order"]
        )
        op.create_index(
            "uq_cost_components_org_name_active",
            "cost_components",
            ["organisation_id", "name"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
        op.create_index(
            "uq_cost_components_org_report_key_self",
            "cost_components",
            ["organisation_id", "report_key"],
            unique=True,
            postgresql_where=sa.text("kind = 'self_reported' AND deleted_at IS NULL"),
        )

    # 3-7. The FIVE pinned migration-surface columns (round-trip asserted).
    op.add_column("runs", sa.Column("cost_breakdown", sa.JSON(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("ledger_written", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("runs", sa.Column("ledger_refused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "org_daily_run_counts",
        sa.Column("clamped", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "org_daily_run_counts",
        sa.Column("refused_spend_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
    )

    # 9/9a. The two blocking CREATE INDEX on the hottest table (maintenance
    #        window flag in the docstring). Plain CREATE INDEX — no CONCURRENTLY.
    if pg:
        op.create_index("ix_runs_probe", "runs", ["organisation_id", "started_at"])
        op.create_index("ix_runs_refusal", "runs", ["organisation_id", "created_at"])

    # 12-13. RLS enable + policy (the 0008_rls_pipeline_folders pattern) and
    #        the direct PUBLIC table grant (Postgres-only; RLS is the
    #        confinement — the grant is role-agnostic by design and every write
    #        path calls set_rls_org).
    if pg:
        op.execute("ALTER TABLE cost_components ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON cost_components USING ({_STRICT_RLS})")
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON cost_components TO PUBLIC")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("DROP POLICY IF EXISTS rls_org_isolation ON cost_components")
        op.execute("ALTER TABLE cost_components DISABLE ROW LEVEL SECURITY")
        op.drop_index("uq_cost_components_org_report_key_self", table_name="cost_components")
        op.drop_index("uq_cost_components_org_name_active", table_name="cost_components")
        op.drop_index("ix_cost_components_org_enabled_sort", table_name="cost_components")

    op.drop_table("cost_components")

    op.drop_column("runs", "cost_breakdown")
    op.drop_column("runs", "ledger_written")
    op.drop_column("runs", "ledger_refused_at")
    op.drop_column("org_daily_run_counts", "clamped")
    op.drop_column("org_daily_run_counts", "refused_spend_usd")

    if pg:
        op.drop_index("ix_runs_refusal", table_name="runs")
        op.drop_index("ix_runs_probe", table_name="runs")
        op.drop_index("uq_org_daily_run_counts", table_name="org_daily_run_counts")
        op.create_unique_constraint(
            "uq_org_daily_run_counts_org_team_date",
            "org_daily_run_counts",
            ["organisation_id", "team_id", "run_date"],
        )
