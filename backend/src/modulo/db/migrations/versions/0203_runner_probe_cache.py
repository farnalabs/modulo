"""runner_probe_cache — cached per-(org, machine) runner health probe (FAR-591 / D5).

Revision ID: 0203_runner_probe_cache
Revises: 0195_hitl_claim_gate_config_json
Create Date: 2026-09-09

What this migration does:

* ``runner_probe_cache`` — org-scoped cache table keyed
  ``(organisation_id, machine_id)`` (UNIQUE). The per-machine system-cron
  health probe (60s cadence, ADR 029) upserts one row per deployment
  machine: engine reachability, worst-of pinned-image presence, per-image
  detail, engine ``/info`` resources, and the probe timestamp. The Runners
  page + node editor read this cache — never a synchronous probe on the
  request path.
* RLS — ENABLE + FORCE ROW LEVEL SECURITY + ``rls_org_isolation`` on the
  table (owner ``modulo_migrate``), same ceremony as the other OrgScoped
  tables. The ``OrgScoped`` ORM mixin alone is insufficient: the app role
  must not be able to read a cross-org probe row.

Aditively reversible: downgrade drops the table + RLS grants/policies.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0203_runner_probe_cache"
down_revision: str | None = "0195_hitl_claim_gate_config_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "runner_probe_cache"

# Module-level tuple of org-scoped tables this migration secures with RLS, so the
# architecture test (test_rls_coverage) can detect the coverage without parsing
# the f-string DDL below.
_ORG_SCOPED_TABLES = (_TABLE,)


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
        system_role = _role_exists(bind, _SYSTEM_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
    else:
        migrate_role = False
        app_role = False
        system_role = False

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("machine_id", sa.String(length=255), nullable=False),
        sa.Column("engine_reachable", sa.Boolean(), nullable=False),
        sa.Column("images_present", sa.Boolean(), nullable=True),
        sa.Column("image_checks_json", sa.JSON(), nullable=False),
        sa.Column("engine_info_json", sa.JSON(), nullable=False),
        sa.Column("probe_error", sa.String(length=500), nullable=True),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "machine_id", name="uq_runner_probe_cache_org_machine"),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    op.create_index("ix_runner_probe_cache_organisation_id", _TABLE, ["organisation_id"])
    op.create_index("ix_runner_probe_cache_machine_id", _TABLE, ["machine_id"])

    if pg:
        op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")
        # qa F14: EXPLICIT system-role privileges (0192 precedent) — the
        # probe cron writes the cache as modulo_system (BYPASSRLS skips row
        # security but NOT table privileges). A missing grant wedges every
        # org's probe row write: the cache never refreshes and every strip
        # ages to "status unknown".
        if system_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_SYSTEM_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_TABLE}")
        op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_runner_probe_cache_machine_id", table_name=_TABLE)
    op.drop_index("ix_runner_probe_cache_organisation_id", table_name=_TABLE)

    op.drop_table(_TABLE)
