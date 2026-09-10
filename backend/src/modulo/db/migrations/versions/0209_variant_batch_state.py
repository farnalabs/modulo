"""variant_batch_state table (FAR-775).

Revision ID: 0209_variant_batch_state
Revises: 0208_notification_indexes_and_constraint
Create Date: 2026-09-10

Adds a lightweight persistence row for variant batch metadata: name,
pipeline_id, variant_group_id, input_payload, organisation_id, and
soft-delete support (deleted_at). Batches predating this feature have no
stored row — the route synthesizes detail/list from runs themselves.

New-table ceremony per the 0066/0204/0207 precedent: SET ROLE modulo_migrate
around creation so the table is owned by the migrate role (NOT modulo_app —
an app-owned RLS-FORCED table lets the app bypass its own RLS), then
FORCE ROW LEVEL SECURITY + rls_org_isolation + DML grants to modulo_app
and modulo_system. Ceremony is role-existence guarded (fresh dev/BDD DBs
have no custom roles).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from modulo.db.migrations._rls_ceremony import (
    assert_owner_is_migrate as _assert_owner_is_migrate,
)
from modulo.db.migrations._rls_ceremony import (
    is_postgres as _is_postgres,
)
from modulo.db.migrations._rls_ceremony import (
    role_exists as _role_exists,
)

revision: str = "0209_variant_batch_state"
down_revision: str | None = "0208_notification_indexes_and_constraint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"
_TABLE = "variant_batch_state"
# Declared so the RLS-coverage drift test can detect this table's ENABLE ROW LEVEL
# SECURITY migration (the DDL below is emitted via an f-string, which the test's
# literal regex intentionally does not match).
_ORG_SCOPED_TABLES = (_TABLE,)
_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    json_type = sa.JSON()
    if pg:
        from sqlalchemy.dialects.postgresql import JSONB

        json_type = sa.JSON().with_variant(JSONB(), "postgresql")

    migrate_role = app_role = system_role = False
    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        system_role = _role_exists(bind, _SYSTEM_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        _TABLE,
        sa.Column(
            "batch_id",
            sa.Uuid(),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "pipeline_id",
            sa.Uuid(),
            nullable=True,
        ),
        sa.Column(
            "variant_group_id",
            sa.Uuid(),
            nullable=True,
        ),
        sa.Column(
            "input_payload",
            json_type,
            nullable=True,
            server_default="{}",
        ),
        sa.Column(
            "organisation_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            ondelete="CASCADE",
            name="fk_variant_batch_state_organisation_id",
        ),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    op.create_index("ix_variant_batch_state_organisation_id", _TABLE, ["organisation_id"])

    if pg:
        op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")
        if system_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_SYSTEM_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_TABLE}")
        op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_variant_batch_state_organisation_id", table_name=_TABLE)
    op.drop_table(_TABLE)
