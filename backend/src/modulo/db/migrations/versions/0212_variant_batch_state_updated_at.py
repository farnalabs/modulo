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


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    # 0211_variant_batch_state now also declares ``updated_at`` in its
    # create_table (added by #363), so on a fresh DB the column already
    # exists. This migration therefore only adds it when it is genuinely
    # missing (e.g. a production DB that applied the pre-#363 0211) —
    # making it idempotent and fixing the DuplicateColumn failure that
    # occurred when both 0211 and 0212 tried to add the column.
    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN in existing:
        return

    migrate_role = _role_exists(bind, _MIGRATE_ROLE) if pg else False

    # Only SET ROLE where the table is already owned by modulo_migrate (prod);
    # on a fresh DB the caller owns the table and SET ROLE would fail the ADD.
    migrate_owns_table = bool(pg and migrate_role and _table_owner(bind, _TABLE) == _MIGRATE_ROLE)

    if migrate_owns_table:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

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

    # Symmetric guard: only drop the column if this migration actually
    # added it (it is absent on a fresh DB where 0211 owns the column).
    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN not in existing:
        return

    op.drop_column(_TABLE, _COLUMN)
