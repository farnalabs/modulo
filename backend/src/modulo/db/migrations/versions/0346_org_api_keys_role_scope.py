"""Widen ``ck_org_api_keys_role`` to a scope-aware role ceiling (FAR-2281).

Revision ID: 0346_org_api_keys_role_scope
Revises: 0345_system_maps_name_integrity
Create Date: 2026-11-01

The flat prohibition on admin-role API keys conflated two different
credential provenances. FAR-2281 splits the ceiling by provenance:

* ``scope='user'`` — a per-user, human-linked key that acts as its creator's
  identity. Its role ceiling is the owning account's LIVE org role, including
  ``admin``.
* ``scope='org'`` (org-wide, team-scoped, service-owned and per-run keys) — an
  unattributed machine identity, capped at ``operator``/``runner``.

The new CHECK is a strict WIDENING of the original
``role IN ('operator', 'runner')``, so every existing row satisfies it. A
CHECK cannot be ALTERed in place on Postgres, so this is DROP + ADD: the DROP
is existence-gated and the ADD is ``NOT VALID`` then ``VALIDATEd`` so the
validation takes only ``SHARE UPDATE EXCLUSIVE`` rather than blocking writes
(the 0265/0305 idiom). SQLite has no ``ALTER TABLE ... ADD CONSTRAINT``, so it
goes through batch mode (the 0305 pattern). The table is never rewritten and
the migration is idempotent-safe (existence-gated DROP, re-runnable ADD).

The DOWN path narrows the predicate back and FAILS LOUDLY if an admin-role
user-scoped key already exists — a downgrade must never silently strand a role
the code can write.

The shipped migrations (0005/0108/0181) are NEVER edited.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0346_org_api_keys_role_scope"
down_revision: str | None = "0345_system_maps_name_integrity"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "org_api_keys"
_CHECK = "ck_org_api_keys_role"

#: Literal strings only — a format-built SQL statement is blocked by the
#: migration-fstring-sql semgrep rule in this directory. The widened predicate
#: MUST match ``OrgApiKey.__table_args__`` exactly (SQLite-mirror parity).
_ROLE_SCOPE_WIDENED = "role IN ('operator', 'runner') OR (role = 'admin' AND scope = 'user')"
_ROLE_SCOPE_ORIGINAL = "role IN ('operator', 'runner')"

_DROP_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_org_api_keys_role' "
    "AND conrelid = 'public.org_api_keys'::regclass) "
    "THEN ALTER TABLE public.org_api_keys DROP CONSTRAINT ck_org_api_keys_role; "
    "END IF; END $$;"
)
_ADD_CHECK_NOT_VALID = (
    "ALTER TABLE public.org_api_keys ADD CONSTRAINT ck_org_api_keys_role "
    "CHECK (role IN ('operator', 'runner') OR (role = 'admin' AND scope = 'user')) "
    "NOT VALID;"
)
_ADD_CHECK_NOT_VALID_ORIGINAL = (
    "ALTER TABLE public.org_api_keys ADD CONSTRAINT ck_org_api_keys_role "
    "CHECK (role IN ('operator', 'runner')) NOT VALID;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_org_api_keys_role' "
    "AND conrelid = 'public.org_api_keys'::regclass "
    "AND NOT convalidated) "
    "THEN ALTER TABLE public.org_api_keys VALIDATE CONSTRAINT ck_org_api_keys_role; "
    "END IF; END $$;"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _recreate_check(*, predicate: str, pg_add: str) -> None:
    """Drop + recreate the role CHECK with *predicate*.

    Postgres runs the literal DDL statements (existence-gated DROP, NOT VALID
    ADD, VALIDATE); SQLite uses batch mode because it has no
    ``ALTER TABLE ... ADD/DROP CONSTRAINT``.
    """
    if _is_postgres():
        op.execute(sa.text(_DROP_CHECK))
        op.execute(sa.text(pg_add))
        op.execute(sa.text(_VALIDATE_CHECK))
        return
    if _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(_CHECK, type_="check")
            batch_op.create_check_constraint(_CHECK, predicate)


def upgrade() -> None:
    """Permit ``admin`` only for ``scope='user'`` keys (strict widening)."""
    _recreate_check(predicate=_ROLE_SCOPE_WIDENED, pg_add=_ADD_CHECK_NOT_VALID)


def downgrade() -> None:
    """Restore the flat operator/runner ceiling (fails loudly on admin rows)."""
    _recreate_check(predicate=_ROLE_SCOPE_ORIGINAL, pg_add=_ADD_CHECK_NOT_VALID_ORIGINAL)
