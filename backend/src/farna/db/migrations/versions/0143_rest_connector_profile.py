"""REST connector profile table (FAR-412).

Revision ID: 0143_rest_connector_profile
Revises: 0142_merge_heads_add_fk_indexes
Create Date: 2026-08-25

FAR-412 gates the generic REST integration connector (FAR-408). The connector
instance itself already stores its declarative endpoint configuration in
``connector_instances.config_json`` (base_url, method, path, headers, params,
body, records_path, next_cursor_path, passthrough, idempotency_header) and its
credentials as an encrypted multi-field JSON dict in ``credentials_ciphertext``.

This migration adds a **structured profile** for REST connector instances — the
auth/runtime knobs the connection profile surfaces to the operator. The table is
1:1 with ``connector_instances``.

IMPORTANT — NO ``ALTER TYPE ... ADD VALUE`` IS REQUIRED. ``connector_type_id``
on ``connector_instances`` is a ``varchar(255)`` column, NOT a Postgres enum
type. The ``ConnectorType.REST`` value is a Python ``StrEnum`` member that maps
to the string ``"rest"``; the DB needs no enum change. Because there is no enum
type, there is no "cannot run ALTER TYPE inside a transaction block" ordering
constraint to work around — the table is created entirely within Alembic's
normal transaction.

The credential *secret* (token / api_key / password) deliberately stays in
``connector_instances.credentials_ciphertext`` — it is NOT duplicated here. This
table stores only the non-secret auth mode + runtime knobs, so there is a single
source of truth for the secret.

ROLE WIRING (the 0137 ceremony, verbatim): the migration connects via
``DATABASE_ADMIN_URL``. ``modulo_migrate`` is NOLOGIN so the migration executes
``SET ROLE modulo_migrate`` ONLY around the ``create_table``, then ``RESET ROLE``
IMMEDIATELY AFTER (the RLS-enable + policy + grant steps run as the migration's
caller). A post-create ownership assertion verifies the table owner is
``modulo_migrate`` (the app role must not own it — the owner bypasses RLS).

Reversible: downgrade drops the ``connector_profiles`` table and disables RLS.
Because this is a NEW table carrying no backfill, downgrade is total.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0143_rest_connector_profile"
down_revision: str | None = "0142_merge_heads_add_fk_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "connector_profiles"


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
            "(the app role must NOT own connector_profiles — the owner bypasses RLS)"
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
            op.execute(f"GRANT REFERENCES ON TABLE public.connector_instances TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
    else:
        migrate_role = False
        app_role = False

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("connector_instance_id", sa.Uuid(), nullable=False),
        sa.Column("auth_mode", sa.String(length=20), nullable=False),
        sa.Column("auth_in", sa.String(length=10), nullable=True),
        sa.Column("auth_query_param_name", sa.String(length=128), nullable=True),
        sa.Column("idempotent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("idempotency_key_header", sa.String(length=128), nullable=True),
        sa.Column("response_max_bytes", sa.Integer(), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connector_instance_id"], ["connector_instances.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connector_instance_id", name="uq_connector_profiles_connector_instance"),
        sa.CheckConstraint(
            "auth_mode IN ('bearer', 'api_key', 'basic')",
            name="ck_connector_profiles_auth_mode",
        ),
        sa.CheckConstraint(
            "auth_in IS NULL OR auth_in IN ('header', 'query')",
            name="ck_connector_profiles_auth_in",
        ),
        sa.CheckConstraint(
            "auth_in <> 'query' OR auth_query_param_name IS NOT NULL",
            name="ck_connector_profiles_auth_query_param",
        ),
        sa.CheckConstraint(
            "auth_in <> 'header' OR auth_query_param_name IS NULL",
            name="ck_connector_profiles_auth_header_inverse",
        ),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    # Only org_id gets a plain index here; the connector_instance_id column is
    # covered by the UNIQUE constraint above (its backing index is the single
    # index on that column — a dual unique-constraint + explicit-index pair was
    # redundant, FAR-412).
    op.create_index("ix_connector_profiles_organisation_id", _TABLE, ["organisation_id"])

    if pg:
        op.execute('ALTER TABLE "connector_profiles" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "connector_profiles" FORCE ROW LEVEL SECURITY')
        op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    if pg:
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_TABLE}")
        op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_connector_profiles_organisation_id", table_name=_TABLE)

    op.drop_table(_TABLE)
