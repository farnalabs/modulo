"""Add the ORM-declared ``collection_install_id`` index on entity tables (FAR-762/FAR-761 drift).

Revision ID: 0209_collection_install_id_entity_columns
Revises: 0208_notification_indexes_and_constraint
Create Date: 2026-09-10

The ``install_service`` (PR #317, FAR-762) stamps ``collection_install_id`` on
every entity a collection install writes — see
``core/library_service/install.py::_stamp_install_id`` (schemas, agents,
pipelines) and ``uninstall.py`` which reads/clears the same column. The ORM
models declare ``collection_install_id`` on ``Schema``, ``Agent`` and
``Pipeline`` (nullable UUID, indexed). Migration ``0207_collection_install_tracking``
already adds the *column* (idempotently, ``ADD COLUMN IF NOT EXISTS``) to those
entity tables, but never creates the *index* the ORM declares on it — that is
what this migration is responsible for.

Because ``0207`` may have already created the column (and because this migration
must be safe to re-run), both the column add and the index create are guarded
by existence checks: the column is only added when genuinely absent, and the
index is only created when it does not yet exist. On a database that has applied
``0207`` the column step is a no-op and only the index is added.

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


def _column_exists(bind, table: str, column: str, pg: bool) -> bool:
    """Return True if ``column`` already exists on ``table`` (Postgres + SQLite)."""
    if pg:
        return (
            bind.execute(
                sa.text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).first()
            is not None
        )
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def _index_exists(bind, table: str, index: str, pg: bool) -> bool:
    """Return True if ``index`` already exists on ``table`` (Postgres + SQLite)."""
    if pg:
        return (
            bind.execute(
                sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t AND indexname = :i"),
                {"t": table, "i": index},
            ).first()
            is not None
        )
    rows = bind.execute(sa.text(f"PRAGMA index_list({table})")).fetchall()
    return any(r[1] == index for r in rows)


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

        # Migration 0207_collection_install_tracking already adds this column
        # (idempotently) to the entity tables. Only add it here if it is genuinely
        # absent, otherwise the upgrade crashes with "column already exists" on a
        # database that has already applied 0207 (the same applies to the index
        # that only this migration is responsible for).
        if not _column_exists(bind, table, _COLUMN, pg):
            op.add_column(
                table,
                sa.Column(_COLUMN, sa.Uuid(), nullable=True),
            )
        if not _index_exists(bind, table, f"ix_{table}_{_COLUMN}", pg):
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
        if _index_exists(bind, table, f"ix_{table}_{_COLUMN}", pg):
            op.drop_index(f"ix_{table}_{_COLUMN}", table_name=table)
        if _column_exists(bind, table, _COLUMN, pg):
            op.drop_column(table, _COLUMN)
