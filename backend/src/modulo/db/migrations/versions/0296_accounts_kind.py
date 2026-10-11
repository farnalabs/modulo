"""FAR-1385 slice 1: ``accounts.kind`` principal classification (ADR 056/058).

Every actor in Modulo is a principal. ``accounts.kind`` classifies a row as a
human person (``'human'``) or a non-human **service principal** (``'service'``).
It is a NOT NULL column with a ``'human'`` server default, so every pre-existing
row reads exactly as before and the migration is byte-identical for humans.

Three structural facts land together with the column:

* ``ck_accounts_kind`` — the vocabulary is exactly ``('human', 'service')``.
* ``ck_accounts_service_privilege`` — a service row can never be a system admin
  or a break-glass account (ADR 058: machine principals never hold admin).
* ``email`` and ``auth_provider`` become NULLABLE. Service rows carry NO
  human-shaped identity, so no login / invite / SSO / SCIM / ``/me`` / audit
  lookup can reach one by email, and there is no auth provider to spoof.
  ``sso_subject``, ``password_hash`` and ``last_login`` are already nullable.
  The UNIQUE constraint on ``accounts.email`` tolerates any number of NULLs in
  both PostgreSQL and SQLite, so no index change is needed.

No service row can be CREATED in this slice (that is slice 2's mint surface),
so the downgrade is only unsafe once service rows exist — and it refuses then
rather than resurrecting a NOT NULL violation or a privileged service row.

Deploy ordering (ADR 030): migrate FIRST, then roll code. Old code ignores the
column; the nullable relaxation and the new CHECK constraints are inert for
human rows.

Revision ID: 0296_accounts_kind
Revises: 0295_decision_record_event_identity
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0296_accounts_kind"
down_revision: str | None = "0295_decision_record_event_identity"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "accounts"
_KIND_CHECK = "ck_accounts_kind"
_SERVICE_PRIVILEGE_CHECK = "ck_accounts_service_privilege"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="human",
            comment="Principal kind: 'human' (default) or 'service'",
        ),
    )
    op.create_check_constraint(_KIND_CHECK, _TABLE, "kind IN ('human', 'service')")
    op.create_check_constraint(
        _SERVICE_PRIVILEGE_CHECK,
        _TABLE,
        "kind = 'human' OR (NOT is_system_admin AND NOT is_break_glass)",
    )
    # Human-shaped identity columns are nullable; service rows leave them NULL.
    op.alter_column(_TABLE, "email", existing_type=sa.String(length=320), nullable=True)
    op.alter_column(_TABLE, "auth_provider", existing_type=sa.String(length=20), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    service_rows = bind.execute(sa.text("SELECT COUNT(*) FROM accounts WHERE kind = 'service'")).scalar()
    if service_rows:
        raise RuntimeError(
            f"Refusing to downgrade 0296: {service_rows} accounts row(s) are service principals. "
            "Restoring NOT NULL on email/auth_provider and dropping the privilege CHECK would "
            "silently delete every service principal (and could resurrect a privileged one). "
            "Decommission all service accounts first, then retry."
        )
    null_emails = bind.execute(sa.text("SELECT COUNT(*) FROM accounts WHERE email IS NULL")).scalar()
    if null_emails:
        raise RuntimeError(
            f"Refusing to downgrade 0296: {null_emails} accounts row(s) have a NULL email. "
            "Restoring NOT NULL would fail. Backfill or remove those rows first."
        )
    op.alter_column(_TABLE, "auth_provider", existing_type=sa.String(length=20), nullable=False, server_default="local")
    op.alter_column(_TABLE, "email", existing_type=sa.String(length=320), nullable=False)
    op.drop_constraint(_SERVICE_PRIVILEGE_CHECK, _TABLE, type_="check")
    op.drop_constraint(_KIND_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "kind")
