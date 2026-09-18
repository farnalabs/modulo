"""Add the ``updated_at`` column to ``variant_batch_state`` (FAR-775 drift).

Revision ID: 0212_variant_batch_state_updated_at
Revises: 0211_variant_batch_state
Create Date: 2026-09-11

The ``VariantBatchState`` model uses ``TimestampMixin``, which declares both
``created_at`` and ``updated_at``. Migration ``0211_variant_batch_state`` does
NOT create ``updated_at`` — main ``#371`` (ddc423a) removed it from ``0211`` to
avoid a double-add race — so this migration is the one that adds the column and
brings the migrated schema in line with the ORM metadata (verified by
``tests/integration/test_initial_migration.py::test_migrated_schema_matches_orm_metadata``).

The ADD is guarded with ``IF NOT EXISTS`` so it is a no-op on production DBs that
applied an *older* ``0211`` which did declare ``updated_at`` (those predate the
``#371`` removal). That keeps the chain idempotent across fresh DBs and production
replays alike. The column mirrors the ``TimestampMixin`` declaration:
``DateTime(timezone=True)``, ``nullable=False``, ``server_default=now()``.

Ownership ceremony (the 0066/0134 pattern, in spirit from 0209): on a DB where
the table is already owned by ``modulo_migrate`` (production, bootstrap ran
before alembic), ``SET ROLE modulo_migrate`` before the guarded ``ADD COLUMN`` so
column ownership stays with the migrate role; on a fresh DB where the caller owns
the table the ceremony is skipped. No RLS/policy change — the table already
carries org-isolation RLS + DML grants from ``0211``.
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

    # 0211_variant_batch_state does NOT declare ``updated_at`` (main #371 removed
    # it), so on a fresh DB the column is genuinely missing and this migration
    # adds it. The guard below also makes the migration a no-op on production DBs
    # that applied the older 0211 (which DID declare ``updated_at``), preventing
    # the DuplicateColumn failure that occurs when both the table DDL and this
    # ADD try to create the same column.
    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN in existing:
        return

    migrate_role = _role_exists(bind, _MIGRATE_ROLE) if pg else False

    # Only SET ROLE where the table is already owned by modulo_migrate (prod);
    # on a fresh DB the caller owns the table and SET ROLE would fail the ADD.
    migrate_owns_table = bool(pg and migrate_role and _table_owner(bind, _TABLE) == _MIGRATE_ROLE)

    if migrate_owns_table:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    # Guarded DDL: 0211_variant_batch_state does NOT create updated_at (main #371
    # removed it), so this follow-up is the one that adds the column. The ADD COLUMN
    # IF NOT EXISTS keeps the chain idempotent across fresh DBs (column absent) and
    # production replays that applied an older 0211 (column already present).
    op.execute(
        f'ALTER TABLE public."{_TABLE}" '
        f'ADD COLUMN IF NOT EXISTS "{_COLUMN}" timestamp with time zone '
        f"NOT NULL DEFAULT now()"
    )

    if pg and migrate_owns_table:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)


def downgrade() -> None:
    # updated_at is owned by 0211_variant_batch_state (created via create_table);
    # this reconciliation follow-up must not drop a column it does not own.
    # Downgrade is a deliberate no-op (reconciliation is not reversible in
    # general), matching the guarded ADD COLUMN IF NOT EXISTS above.
    pass
