"""agent_runner_bindings — per-agent Model Backend env-var bindings (FAR-592 / D6).

Revision ID: 0189_agent_runner_bindings
Revises: 0188_pipeline_run_context_defaults_default
Create Date: 2026-09-06

What this migration does:

* ``agent_runner_bindings`` — normalised, org-scoped child table. Columns:
  ``agent_id`` FK→agents ON DELETE CASCADE, ``model_backend_id`` FK→
  model_backends ON DELETE RESTRICT (the race-proof backstop; the CRUD
  pre-delete inventory reports "in use by N agents"), ``target_env_var``,
  ``source_field``, ``account_id``, timestamps.
  UNIQUE (organisation_id, agent_id, target_env_var) — one var per agent.
* RLS — ENABLE + FORCE ROW LEVEL SECURITY + ``rls_org_isolation`` on the
  table (owner ``modulo_migrate``), same ceremony as the other OrgScoped
  tables. The ``OrgScoped`` ORM mixin alone is insufficient: the app role
  must not be able to read a cross-org binding.
* Org teardown: the model_backends RESTRICT could abort the hard
  ``delete_organisation`` mass-cascade on the binding rows. The delete CRUD
  removes binding rows explicitly BEFORE the org delete (this table has no
  DEFERRABLE constraint so a plain delete-first ordering is race-free for
  org teardown, which already holds the org under the session's own locks).

Aditively reversible: downgrade drops the table + RLS grants/policies.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0189_agent_runner_bindings"
down_revision: str | None = "0188_pipeline_run_context_defaults_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "agent_runner_bindings"

# Module-level tuple of org-scoped tables this migration secures with RLS, so the
# architecture test (test_rls_coverage) can detect the coverage without parsing
# the f-string DDL below.
_ORG_SCOPED_TABLES = ("agent_runner_bindings",)


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
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            for tbl in ("organisations", "agents", "model_backends", "accounts"):
                op.execute(f"GRANT REFERENCES ON TABLE public.{tbl} TO {_MIGRATE_ROLE}")
    else:
        migrate_role = False
        app_role = False

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("model_backend_id", sa.Uuid(), nullable=False),
        sa.Column("target_env_var", sa.String(length=128), nullable=False),
        sa.Column("source_field", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_backend_id"], ["model_backends.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organisation_id",
            "agent_id",
            "target_env_var",
            name="uq_agent_runner_bindings_org_agent_target",
        ),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    op.create_index("ix_agent_runner_bindings_organisation_id", _TABLE, ["organisation_id"])
    op.create_index("ix_agent_runner_bindings_agent_id", _TABLE, ["agent_id"])
    op.create_index("ix_agent_runner_bindings_model_backend_id", _TABLE, ["model_backend_id"])
    op.create_index("ix_agent_runner_bindings_account_id", _TABLE, ["account_id"])

    if pg:
        op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_TABLE}")
        op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_agent_runner_bindings_account_id", table_name=_TABLE)
    op.drop_index("ix_agent_runner_bindings_model_backend_id", table_name=_TABLE)
    op.drop_index("ix_agent_runner_bindings_agent_id", table_name=_TABLE)
    op.drop_index("ix_agent_runner_bindings_organisation_id", table_name=_TABLE)

    op.drop_table(_TABLE)
