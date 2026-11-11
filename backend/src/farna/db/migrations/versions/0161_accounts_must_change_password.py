"""accounts — add ``must_change_password`` flag (FAR-460).

Revision ID: 0161_accounts_must_change_password
Revises: 0160_run_idempotency_key
Create Date: 2026-08-27

Admin actions that hand a user a temporary credential (``admin_reset_password``,
``admin_create_user`` adopting an SSO/SCIM account) previously left the
temporary password as the permanent one — the reset-password UI claimed the
user would be "prompted to change it on next login", but no such mechanism
existed anywhere.

This migration adds the ``accounts.must_change_password`` boolean column
backing that mechanism (FAR-460). The column is additive and nullable-safe:

* ``NOT NULL`` with a ``server_default false`` so existing rows backfill to
  ``false`` without a table rewrite decision on our part — Postgres fills the
  default for every existing row during the single catalog-level rewrite.
* The ORM model default mirrors it so new rows are explicit.

The migration is idempotent: the column add is gated on
``information_schema.columns`` and the drop is ``IF EXISTS``, following the
guarded style used throughout recent migrations.
"""

from __future__ import annotations

from alembic import op

revision: str = "0161_accounts_must_change_password"
down_revision: str | None = "0160_run_idempotency_key"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_UPGRADE_SQL = (
    "DO $$ BEGIN "
    "IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='accounts' "
    "AND column_name='must_change_password') THEN "
    "ALTER TABLE public.accounts ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE; "
    "END IF; END $$;"
)

_DOWNGRADE_SQL = "ALTER TABLE public.accounts DROP COLUMN IF EXISTS must_change_password;"


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
