"""Add the ``updated_at`` column to ``variant_batch_state`` (FAR-775 drift).

Revision ID: 0212_variant_batch_state_updated_at
Revises: 0211_variant_batch_state
Create Date: 2026-09-11

The ``VariantBatchState`` model uses ``TimestampMixin``, which declares both
``created_at`` and ``updated_at``. Migration ``0211_variant_batch_state`` created
the table with ``created_at`` but omitted ``updated_at``, so the migrated schema
drifts from the ORM metadata (caught by
``tests/integration/test_initial_migration.py::test_migrated_schema_matches_orm_metadata``).

Because ``0211`` is already applied on production, this follow-up migration is
the correct place to add the missing column (editing ``0211`` would not re-run on
an existing DB). The column mirrors the ``TimestampMixin`` declaration:
``DateTime(timezone=True)``, ``nullable=False``, ``server_default=now()``.

The ``ADD COLUMN`` is guarded so the migration is idempotent: if ``0211`` already
creates ``updated_at`` (or the migration is re-run), the column is left untouched
rather than raising ``DuplicateColumn``.

Ownership ceremony (the 0066/0134 pattern, in spirit from 0209): on a DB where
the table is already owned by ``modulo_migrate`` (production, bootstrap ran
before alembic), ``SET ROLE modulo_migrate`` before ``ADD COLUMN`` so column
ownership stays with the migrate role; on a fresh DB where the caller owns the
table the ceremony is skipped. No RLS/policy change — the table already carries
org-isolation RLS + DML grants from ``0211``.
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

revision: str = "0212_variant_batch_state_updated_at"
down_revision: str | None = "0211_variant_batch_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_TABLE = "variant_batch_state"
_COLUMN = "updated_at"


def _table_owner(bind, table: str) -> str | None:
    """Return the current owner role of ``table`` (or None if not present)."""
    return bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:oid)"),
        {"oid": f"public.{table}"},
    ).scalar_one_or_none()


def _column_exists(bind, table: str, column: str) -> bool:
    """Return True if ``column`` already exists on ``table``."""
    if bind.dialect.name == "postgresql":
        return bool(
            bind.execute(
                sa.text("SELECT 1 FROM information_schema.columns WHERE table_name = :table AND column_name = :column"),
                {"table": table, "column": column},
            ).first()
        )
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
    else:
        migrate_role = False

    # Only SET ROLE where the table is already owned by modulo_migrate (prod);
    # on a fresh DB the caller owns the table and SET ROLE would fail the ADD.
    migrate_owns_table = bool(pg and migrate_role and _table_owner(bind, _TABLE) == _MIGRATE_ROLE)

    if migrate_owns_table:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    # Idempotent: 0211 may already create updated_at (or the migration may be
    # re-run), so only add the column when it is genuinely missing.
    if not _column_exists(bind, _TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if pg and migrate_owns_table:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    if _column_exists(bind, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
