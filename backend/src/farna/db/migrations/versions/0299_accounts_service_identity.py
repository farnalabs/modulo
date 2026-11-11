"""FAR-1385: structurally enforce that a service row has no human identity.

``0298_accounts_kind`` made the human-shaped identity columns NULLABLE so a
service principal can carry no synthetic email/password/SSO subject, but
nothing FORCED them to be NULL — the model comment claimed NULL identity was
"the structural reason no login path can reach one" while only a convention
enforced it. This migration adds ``ck_accounts_service_identity``:

    kind = 'human' OR (email IS NULL AND password_hash IS NULL AND sso_subject IS NULL)

so a service row can never hold an email (no invite/SSO/SCIM/login lookup can
reach it), a password hash (nothing to verify) or an SSO subject (nothing to
match). This is what makes the SSO JIT chain (``auth/sso.py``) unreachable for
a service row at the DB level, rather than relying on the email lookup merely
missing a NULL.

Every pre-existing row is ``kind='human'`` (the 0298 server default), so the
CHECK is satisfiable by the whole table and the migration is safe. The upgrade
still refuses loudly if a service row with human identity somehow exists rather
than failing with an opaque constraint error.

Deploy ordering (ADR 030): migrate FIRST, then roll code.

Revision ID: 0299_accounts_service_identity
Revises: 0298_accounts_kind
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0299_accounts_service_identity"
down_revision: str | None = "0298_accounts_kind"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "accounts"
_IDENTITY_CHECK = "ck_accounts_service_identity"
_IDENTITY_PREDICATE = "kind = 'human' OR (email IS NULL AND password_hash IS NULL AND sso_subject IS NULL)"


def upgrade() -> None:
    bind = op.get_bind()
    offending = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM accounts "
            "WHERE kind = 'service' AND (email IS NOT NULL OR password_hash IS NOT NULL OR sso_subject IS NOT NULL)"
        )
    ).scalar()
    if offending:
        raise RuntimeError(
            f"Refusing to apply 0299: {offending} service account row(s) still carry an email, "
            "password_hash or sso_subject. The identity CHECK cannot be satisfied for those rows; "
            "null the human-shaped identity first, then retry."
        )
    op.create_check_constraint(_IDENTITY_CHECK, _TABLE, _IDENTITY_PREDICATE)


def downgrade() -> None:
    op.drop_constraint(_IDENTITY_CHECK, _TABLE, type_="check")
