"""EvalDataset / EvalCase — repeatable eval input corpus (FAR-375 Phase 2).

Revision ID: 0131_eval_dataset_corpus
Revises: 0130_eval_suite_entity
Create Date: 2026-08-24

Phase 2 of the Generic Eval Product MVP: a managed, versioned input corpus that
an eval suite (Phase 3) runs against, decoupled from ``Run`` retention so
repeatable re-runs survive Run pruning. This is the **data layer only** — no
endpoints, UI, or run-execution logic (that is Phase 3).

Two tables:

* ``eval_datasets`` — named, versioned, org- (or team-) scoped corpus header.
  Soft-delete only (deleted_at / deleted_by); one active name per org.
* ``eval_cases`` — the individual repeatable inputs. ``input_payload`` is the
  canonical payload store (mirrors ``webhook_payloads.raw_payload``) held
  DATA-ONLY and verbatim; ``expected_output`` is an optional reference answer;
  ``input_hash`` is SHA-256 of the payload for dedupe/audit. A case references
  its dataset with ``ON DELETE RESTRICT`` so a referenced dataset can never be
  hard-removed.

ROLE WIRING (the 0066 ceremony, verbatim from 0115): the migration connects via
``DATABASE_ADMIN_URL`` (the superuser/owner URL). ``modulo_migrate`` is a
NOLOGIN role, so it cannot be CONNECTED to — the migration executes
``SET ROLE modulo_migrate`` BEFORE each ``op.create_table(...)``, then
``RESET ROLE`` AFTER (the RLS-enable + policy + grant steps run as the
migration's caller). A post-create ownership assertion verifies each created
table's owner is ``modulo_migrate`` (owner-bypasses-RLS precondition — the app
role must NOT own these tables).

The ceremony is conditional on the roles existing (checked via ``pg_roles``):
on a fresh DB where ``alembic upgrade heads`` runs BEFORE the app bootstraps
roles (e.g. the BDD suite), the GRANTs / ``SET ROLE`` / owner assertions are
skipped and the tables are created by the migration caller.

RLS: ENABLE + FORCE ROW LEVEL SECURITY on both tables (the owner is
``modulo_migrate`` and must NOT bypass RLS). A single org-scoped
``rls_org_isolation`` policy (USING organisation_id = current org) confines all
commands; ``modulo_app`` is granted full DML.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0131_eval_dataset_corpus"
down_revision: str | None = "0130_eval_suite_entity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLES = ("eval_datasets", "eval_cases")


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    """Return True when the Postgres role exists (fresh dev/BDD DBs have none)."""
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    """POST-CREATE ownership assertion — the 0066 ceremony (before RLS)."""
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl=f"public.{table}"
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"{table} owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own eval corpus tables — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.teams TO {_MIGRATE_ROLE}")
    else:
        migrate_role = False
        app_role = False

    # Idempotency (FAR-374 deploy fix): this migration may be re-run on a DB where
    # ``alembic upgrade heads`` already created the corpus tables and only the
    # ``alembic_version`` marker was rewound (e.g. the eval-suite migration test's
    # restore-upgrade, or a head-conflict repair). ``CREATE TABLE IF NOT EXISTS``
    # below makes the DDL re-runnable; the ownership assertion must only run for
    # tables THIS run actually created — a pre-existing corpus table is already
    # owned by the prior run, and asserting against it again via a NOLOGIN role
    # would false-fail.
    datasets_created = not (pg and sa.inspect(bind).has_table("eval_datasets"))
    cases_created = not (pg and sa.inspect(bind).has_table("eval_cases"))

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        "eval_datasets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), nullable=False, server_default="org"),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_eval_datasets_visibility"),
        if_not_exists=True,
    )

    op.create_table(
        "eval_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("expected_output", sa.JSON(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        # RESTRICT: a referenced dataset can never be hard-deleted.
        sa.ForeignKeyConstraint(["dataset_id"], ["eval_datasets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        if datasets_created:
            _assert_owner_is_migrate(bind, "eval_datasets")
        if cases_created:
            _assert_owner_is_migrate(bind, "eval_cases")

    # Indexes (active-only uniqueness) + FK lookup helpers. if_not_exists makes
    # the set re-runnable when the tables already exist on disk.
    op.create_index("ix_eval_datasets_organisation_id", "eval_datasets", ["organisation_id"], if_not_exists=True)
    op.create_index("ix_eval_datasets_owner_team_id", "eval_datasets", ["owner_team_id"], if_not_exists=True)
    op.create_index(
        "uq_eval_datasets_org_name_active",
        "eval_datasets",
        ["organisation_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        if_not_exists=True,
    )

    op.create_index("ix_eval_cases_organisation_id", "eval_cases", ["organisation_id"], if_not_exists=True)
    op.create_index("ix_eval_cases_dataset_id", "eval_cases", ["dataset_id"], if_not_exists=True)
    op.create_index("ix_eval_cases_input_hash", "eval_cases", ["input_hash"], if_not_exists=True)
    op.create_index(
        "uq_eval_cases_dataset_hash_active",
        "eval_cases",
        ["dataset_id", "input_hash"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        if_not_exists=True,
    )

    if pg:
        for table in _TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            # DROP-then-CREATE keeps this re-runnable (CREATE POLICY has no IF NOT EXISTS).
            op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {table}")
            op.execute(f"CREATE POLICY rls_org_isolation ON {table} USING ({_ORG_SCOPE})")
        if app_role:
            for table in _TABLES:
                op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    if pg:
        for table in _TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {table}")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "uq_eval_cases_dataset_hash_active",
        table_name="eval_cases",
        postgresql_drop_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_eval_cases_input_hash", table_name="eval_cases")
    op.drop_index("ix_eval_cases_dataset_id", table_name="eval_cases")
    op.drop_index("ix_eval_cases_organisation_id", table_name="eval_cases")
    op.drop_index(
        "uq_eval_datasets_org_name_active",
        table_name="eval_datasets",
        postgresql_drop_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_eval_datasets_owner_team_id", table_name="eval_datasets")
    op.drop_index("ix_eval_datasets_organisation_id", table_name="eval_datasets")

    op.drop_table("eval_cases")
    op.drop_table("eval_datasets")
