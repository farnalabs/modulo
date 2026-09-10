"""Add the ``collection_install_id`` ORM index (FAR-762/FAR-761 drift).

Revision ID: 0209_collection_install_id_entity_columns
Revises: 0208_notification_indexes_and_constraint
Create Date: 2026-09-10

The ``install_service`` (PR #317, FAR-762) stamps ``collection_install_id`` on
every entity a collection install writes — see
``core/library_service/install.py::_stamp_install_id`` (schemas, agents,
pipelines) and ``uninstall.py`` which reads/clears the same column. The ORM
models declare ``collection_install_id`` on ``Schema``, ``Agent`` and
``Pipeline`` (nullable UUID, indexed). On a fresh DB, migration
``0207_collection_install_tracking`` already creates this column (with
``IF NOT EXISTS``) on the entity tables; on a prod DB whose ``0207`` predates
that column add the column is still absent. Either way the column must exist and
carry the index the ORM declares for the ORM↔DB schema to stay in sync.

This migration closes the gap idempotently: it adds the nullable UUID column
only when missing (so it does not collide with ``0207`` on a fresh DB) and
creates the ORM-declared index ``ix_<table>_collection_install_id``. The column
is nullable: an entity may or may not belong to a collection install, and the
audit history already exists in ``collection_install_entity`` (no backfill is
required — every row simply starts NULL, the same as a fresh install never
performed).

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
from sqlalchemy import inspect

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

    insp = inspect(bind)

    for table in _ENTITY_TABLES:
        # Migration 0207_collection_install_tracking already creates this column
        # (with IF NOT EXISTS) on a fresh DB, so re-adding it here would fail the
        # whole chain with "column <table>.collection_install_id already exists"
        # during ``alembic upgrade head`` (seen on the pre-deploy integration-test
        # run). On a prod DB whose 0207 predates the column add, the column is
        # still absent and must be created here. Idempotent existence checks keep
        # the migration correct for both a fresh and an already-applied DB.
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        column_present = _COLUMN in existing_cols
        existing_indexes = {i["name"] for i in insp.get_indexes(table)}
        index_present = f"ix_{table}_{_COLUMN}" in existing_indexes

        # The SET ROLE ownership ceremony only applies where the table is already
        # owned by ``modulo_migrate`` (prod DBs whose earlier migrations ran under
        # the migrate role). On a fresh DB where the migration caller owns the
        # table, SET ROLE to the non-owner ``modulo_migrate`` would make the ALTER
        # below fail with "must be owner of table <table>".
        migrate_owns_table = bool(pg and migrate_role and _table_owner(bind, table) == _MIGRATE_ROLE)

        if migrate_owns_table:
            op.execute(f"SET ROLE {_MIGRATE_ROLE}")

        if not column_present:
            op.add_column(
                table,
                sa.Column(_COLUMN, sa.Uuid(), nullable=True),
            )
        if not index_present:
            op.create_index(f"ix_{table}_{_COLUMN}", table, [_COLUMN])

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
