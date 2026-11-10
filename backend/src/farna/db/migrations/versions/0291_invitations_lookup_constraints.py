"""Add live-invite lookup indexes + domain CHECKs for invitations.

Schema legs:

1. ``ix_invitations_org_email_live`` — partial composite index
   ``(organisation_id, email) WHERE consumed_at IS NULL AND
   revoked_at IS NULL``. The invite-duplicate guard
   (``has_live_for_email``) and the SSO join gate
   (``get_live_for_email``, executed on every SSO login) filter exactly
   ``WHERE organisation_id = ? AND email = ?`` plus the shared
   ``_live_conditions`` scope. The only pre-existing index is the
   single-column ``ix_invitations_organisation_id``, so the email
   equality term has no index support.

2. ``ix_invitations_expires_at_live`` — partial index on
   ``expires_at WHERE consumed_at IS NULL AND revoked_at IS NULL``.
   Every liveness check filters ``expires_at > now()`` and a
   stale-invite purge sweeps on expiry; the column previously had no
   index at all.

3. ``ck_invitations_org_role`` — the role vocabulary
   (admin/operator/runner/viewer) was enforced only at the API boundary
   (``admin.invite-user``); ``crud.create_invitation`` itself does not
   validate, so a bad role would persist unchecked. Now enforced at the
   DB level.

4. ``ck_invitations_token_hash_len`` — ``token_hash`` is always a
   SHA-256 hex digest (``hash_token``); enforce exactly 64 chars.
   ``length()`` is used over ``char_length()`` so the expression parses
   on both Postgres and SQLite (unit-test ``create_all``).

Indexes are created with ``IF NOT EXISTS`` (the guarded style used by
0177) so a re-run never fails. Both legs are Postgres-only: the
``public.``-qualified index SQL has no SQLite equivalent, and
``op.create_check_constraint`` emits a bare ``ALTER TABLE ... ADD
CONSTRAINT`` which SQLite cannot execute. SQLite unit-test schemas get
the same two partial indexes and the same two CHECK rules from the
``Invitation`` model's ``__table_args__`` (``sqlite_where`` predicates +
``CheckConstraint``s) via ``create_all`` (the 0246 precedent), so the
SQLite-ported claim holds at the model/``create_all`` layer, not by
running this migration's DDL there.

Downgrade: drops both indexes and both constraints.

Revision ID: 0291_invitations_lookup_constraints
Revises: 0290_scheduled_reports_due_scan
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0291_invitations_lookup_constraints"
down_revision: str | None = "0290_scheduled_reports_due_scan"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_LIVE_WHERE = "consumed_at IS NULL AND revoked_at IS NULL"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_invitations_org_email_live "
            "ON public.invitations (organisation_id, email) "
            f"WHERE {_LIVE_WHERE}"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_invitations_expires_at_live "
            "ON public.invitations (expires_at) "
            f"WHERE {_LIVE_WHERE}"
        )
    )
    if not _is_postgres():
        return
    op.create_check_constraint(
        "ck_invitations_org_role",
        "invitations",
        sa.text("org_role IN ('admin', 'operator', 'runner', 'viewer')"),
    )
    op.create_check_constraint(
        "ck_invitations_token_hash_len",
        "invitations",
        sa.text("length(token_hash) = 64"),
    )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint("ck_invitations_token_hash_len", "invitations", type_="check")
        op.drop_constraint("ck_invitations_org_role", "invitations", type_="check")
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_invitations_expires_at_live"))
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_invitations_org_email_live"))
