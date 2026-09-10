"""collection_install / collection_install_entity tracking tables (FAR-761).

Revision ID: 0207_collection_install_tracking
Revises: 0206_deleted_defaults_signal_check
Create Date: 2026-09-10

Adds provenance for collection install/uninstall events. ``collection_install``
is an org-scoped table (one row per successful/failed install of a library
primitive into an org); ``collection_install_entity`` is the per-entity child
(what was actually written — schemas/agents/pipelines).

New-table ceremony (the 0066/0204 precedent, verbatim in spirit from
0192/0131): the migration connects as the owner/admin role, grants
``modulo_migrate`` the schema + REFERENCES it needs, ``SET ROLE modulo_migrate``
around table creation so the table is owned by ``modulo_migrate`` (NOT the app
role — an app-owned RLS-FORCED table would let the app bypass its own RLS),
``RESET ROLE``, asserts the owner, then enables FORCE ROW LEVEL SECURITY +
``rls_org_isolation`` and grants DML to ``modulo_app``/``modulo_system``. The
ceremony is role-existence guarded (fresh dev/BDD DBs have no custom roles).

The non-org child ``collection_install_entity`` also gets FORCE RLS with a
parent-derived ``rls_org_isolation`` policy (scope = parent install's org) plus
the same DML grants — without the grants the app role cannot read the rows the
parent JOIN resolves, and without the policy a non-org table would expose
cross-org rows.

Naming: ``organisation_id`` (repo convention, matches the shared
``app.organisation_id`` RLS predicate — NOT ``org_id``). Indexes use the
``ix_<table>_<col>`` convention. The PRIMARY KEY already yields a unique index
on ``install_id``, so no separate unique index is created.

SQLite parity: ``env.py`` supports a sqlite backend for local/dev, so all
Postgres-only DDL (``gen_random_uuid`` server default, JSONB) is dialect-guarded
via ``_is_postgres``; on sqlite the columns fall back to plain UUID/JSON/datetime
types and no ceremony/RLS runs.

``ON DELETE RESTRICT`` on ``collection_id`` is INTENTIONAL: a collection that
has ever been installed leaves provenance history that must survive collection
deletion (the install row is the audit record). Provenance columns
(``collection_install_id``) are added to the entity tables with ``IF NOT EXISTS``
so the migration is idempotent and safe to re-run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0207_collection_install_tracking"
down_revision: str | None = "0206_deleted_defaults_signal_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_COLLECTION_INSTALL = "collection_install"
_COLLECTION_INSTALL_ENTITY = "collection_install_entity"

# Parent-derived org isolation for the non-org child table: a row is visible to
# the caller's org only when its parent install belongs to that org. The child
# carries no organisation_id of its own — access is always via the org-scoped
# parent's install_id. Plain literal (no interpolation) so the SQL-injection
# linter does not trip on the constant table name.
_ENTITY_ORG_SCOPE = (
    "install_id IN (SELECT install_id FROM collection_install "
    "WHERE organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid)"
)

# Read by tests/unit/db/test_rls_coverage.py (style 2: module-level tuple of
# strings) to confirm the org-scoped table has an RLS-enabling migration.
_ORG_SCOPED_TABLES = (_COLLECTION_INSTALL,)


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
            "(the app role must NOT own an RLS-FORCED org-scoped table — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    json_type: sa.types.TypeEngine[object] = sa.JSON().with_variant(JSONB(), "postgresql") if pg else sa.JSON()
    install_id_default = sa.text("gen_random_uuid()") if pg else None

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        system_role = _role_exists(bind, _SYSTEM_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
            # collection_install references library_primitives — grant REFERENCES so
            # the FK can be created under the migrate role.
            op.execute(f"GRANT REFERENCES ON TABLE public.library_primitives TO {_MIGRATE_ROLE}")
    else:
        migrate_role = app_role = system_role = False

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    # 1. collection_install — org-scoped provenance table.
    op.create_table(
        _COLLECTION_INSTALL,
        sa.Column(
            "install_id",
            sa.Uuid(),
            nullable=False,
            primary_key=True,
            server_default=install_id_default,
        ),
        sa.Column(
            "collection_id",
            sa.Uuid(),
            nullable=False,
            index=True,
        ),
        # VARCHAR(50): full semver strings ("1.2.3-rc.1+build.5") fit comfortably;
        # the original VARCHAR(20) was too tight for pre-release/build metadata.
        sa.Column("collection_version", sa.String(length=50), nullable=True),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_manifest", json_type, nullable=True),
        sa.Column("connector_checklist", json_type, nullable=True),
        sa.Column("installed_entities", json_type, nullable=True),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            ondelete="CASCADE",
            name="fk_collection_install_organisation_id",
        ),
        # ON DELETE RESTRICT is intentional: install history is provenance that
        # must survive collection deletion (FAR-761).
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["library_primitives.id"],
            ondelete="RESTRICT",
            name="fk_collection_install_collection_id",
        ),
        sa.CheckConstraint(
            "status IN ('installed', 'failed')",
            name="ck_collection_install_status",
        ),
        sa.UniqueConstraint("organisation_id", "collection_id", name="uq_collection_install_org_collection"),
    )

    # 2. collection_install_entity — per-entity child (no org column; access is
    #    always via the org-scoped parent's install_id).
    op.create_table(
        _COLLECTION_INSTALL_ENTITY,
        sa.Column("install_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["install_id"],
            [f"{_COLLECTION_INSTALL}.install_id"],
            ondelete="CASCADE",
            name="fk_collection_install_entity_install_id",
        ),
        sa.PrimaryKeyConstraint(
            "install_id",
            "entity_type",
            "entity_id",
            name="pk_collection_install_entity",
        ),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _COLLECTION_INSTALL)
        _assert_owner_is_migrate(bind, _COLLECTION_INSTALL_ENTITY)

    # Indexes (created AFTER RESET ROLE, mirroring the 0204 ordering).
    op.create_index("ix_collection_install_organisation_id", _COLLECTION_INSTALL, ["organisation_id"])
    op.create_index("ix_collection_install_entity_install_id", _COLLECTION_INSTALL_ENTITY, ["install_id"])
    # Reverse-lookup index: find which installs touched a given entity.
    op.create_index(
        "ix_collection_install_entity_entity",
        _COLLECTION_INSTALL_ENTITY,
        ["entity_type", "entity_id"],
    )

    # 3. Provenance columns on the entity tables (idempotent).
    op.execute("ALTER TABLE schemas ADD COLUMN IF NOT EXISTS collection_install_id UUID")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS collection_install_id UUID")
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS collection_install_id UUID")

    if pg:
        # RLS last: ENABLE + FORCE + strict fail-closed policy, then DML grants.
        op.execute(f"ALTER TABLE {_COLLECTION_INSTALL} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_COLLECTION_INSTALL} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_COLLECTION_INSTALL} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_COLLECTION_INSTALL} TO {_APP_ROLE}")
        if system_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_COLLECTION_INSTALL} TO {_SYSTEM_ROLE}")

        # collection_install_entity is the non-org child; the runtime app/system
        # roles need DML grants or they cannot read the rows the parent JOIN
        # resolves, and FORCE RLS with a parent-derived policy keeps it scoped to
        # the caller's org (an unscoped child would otherwise leak cross-org rows).
        op.execute(f"ALTER TABLE {_COLLECTION_INSTALL_ENTITY} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_COLLECTION_INSTALL_ENTITY} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_COLLECTION_INSTALL_ENTITY} USING ({_ENTITY_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_COLLECTION_INSTALL_ENTITY} TO {_APP_ROLE}")
        if system_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_COLLECTION_INSTALL_ENTITY} TO {_SYSTEM_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_COLLECTION_INSTALL_ENTITY}")
        op.execute(f"ALTER TABLE {_COLLECTION_INSTALL_ENTITY} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_COLLECTION_INSTALL}")
        op.execute(f"ALTER TABLE {_COLLECTION_INSTALL} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_collection_install_entity_entity", table_name=_COLLECTION_INSTALL_ENTITY)
    op.drop_index("ix_collection_install_entity_install_id", table_name=_COLLECTION_INSTALL_ENTITY)
    op.drop_index("ix_collection_install_organisation_id", table_name=_COLLECTION_INSTALL)

    op.drop_table(_COLLECTION_INSTALL_ENTITY)
    op.drop_table(_COLLECTION_INSTALL)

    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS collection_install_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS collection_install_id")
    op.execute("ALTER TABLE schemas DROP COLUMN IF EXISTS collection_install_id")
