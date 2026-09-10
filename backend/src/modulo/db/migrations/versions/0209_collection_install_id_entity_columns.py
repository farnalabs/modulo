"""Add the ``collection_install_id`` ORM index (FAR-762/FAR-761 drift).

Revision ID: 0209_collection_install_id_entity_columns
Revises: 0208_notification_indexes_and_constraint
Create Date: 2026-09-10

The ``install_service`` (PR #317, FAR-762) stamps ``collection_install_id`` on
every entity a collection install writes — see
``core/library_service/install.py::_stamp_install_id`` (schemas, agents,
pipelines) and ``uninstall.py`` which reads/clears the same column. The ORM
models declare ``collection_install_id`` on ``Schema``, ``Agent`` and
``Pipeline`` (nullable UUID, indexed). The nullable UUID column itself is added
idempotently by migration ``0207_collection_install_tracking`` (the same
migration that adds the ``collection_install`` / ``collection_install_entity``
audit tables). This migration only adds the index each ORM model declares
(``index=True``) — ``ix_<table>_collection_install_id`` on ``schemas``,
``agents`` and ``pipelines`` — which 0207 does not create. An earlier version of
this migration also re-added the column via ``op.add_column`` and failed with
``DuplicateColumn`` on a DB where 0207 had already created it, so the column is
left to 0207 and only the index is created here (idempotently).

ROLE WIRING (the 0134 ceremony, verbatim in spirit from 0066): migrations run
as the ``DATABASE_ADMIN_URL`` superuser, but the org-scoped entity tables are
owned by ``modulo_migrate``. We ``SET ROLE modulo_migrate`` before the
``ALTER TABLE ... ADD COLUMN`` only where the table is already owned by that
role (production, where bootstrap ran before alembic) so ownership stays
consistent; on a fresh DB where the migration caller owns the tables the
ceremony is skipped and the column is added by the caller. The step is
unconditional on the role merely existing — ``SET ROLE`` to a non-owner would
fail the ALTER.

Postgres-only concern: the column/index are plain DDL with no RLS/policy
change (the tables already carry org-isolation RLS + DML grants), so no RLS
step runs. SQLite (used by unit tests via ``Base.metadata.create_all``) has no
role machinery — ``op.add_column`` / ``op.create_index`` run directly there.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from modulo.db.migrations._rls_ceremony import (
    assert_owner_is_migrate as _assert_owner_is_migrate,
)
from modulo.db.migrations._rls_ceremony import is_postgres as _is_postgres
from modulo.db.migrations._rls_ceremony import role_exists as _role_exists

revision: str = "0209_collection_install_id_entity_columns"
down_revision: str | None = "0208_notification_indexes_and_constraint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"

# Entity tables that carry a denormalised install provenance column.
_ENTITY_TABLES = ("schemas", "agents", "pipelines")

_COLUMN = "collection_install_id"


def _table_owner(bind, table: str) -> str | None:
    """Return the current owner role of ``table`` (or None if not present)."""
    return bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:oid)"),
        {"oid": f"public.{table}"},
    ).scalar_one_or_none()


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
    else:
        migrate_role = False

    for table in _ENTITY_TABLES:
        # The SET ROLE ownership ceremony only applies where the table is already
        # owned by ``modulo_migrate`` (prod DBs whose earlier migrations ran under
        # the migrate role). On a fresh DB where the migration caller owns the
        # table, SET ROLE to the non-owner ``modulo_migrate`` would make the ALTER
        # below fail with "must be owner of table <table>".
        migrate_owns_table = bool(pg and migrate_role and _table_owner(bind, table) == _MIGRATE_ROLE)

        if migrate_owns_table:
            op.execute(f"SET ROLE {_MIGRATE_ROLE}")

        # The ``collection_install_id`` column is added (idempotently) by
        # 0207_collection_install_tracking, which runs immediately before this
        # migration. Re-adding it here raised DuplicateColumn on a DB where 0207
        # had already created the column, so this migration only creates the index
        # the ORM models declare (index=True) on the column that already exists.
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_{_COLUMN} ON {table} ({_COLUMN})")

        if pg and migrate_owns_table:
            op.execute("RESET ROLE")
            _assert_owner_is_migrate(bind, table)


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    for table in reversed(_ENTITY_TABLES):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_{_COLUMN}")
