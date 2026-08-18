"""notification_preferences — per-user read-time notification opt-outs (FAR-247).

Revision ID: 0114_notification_preferences
Revises: 0113_guardrail_summary
Create Date: 2026-08-18

Per-user notification opt-outs are enforced at READ time via the shared
``apply_prefs_filter`` helper (db/crud/notifications.py). One row exists per
opted-out ``category`` for a user within an org; a unique constraint on
(organisation_id, account_id, category) keeps the mapping idempotent.

ROLE WIRING (the 0066 ceremony, verbatim): the migration connects via
``DATABASE_ADMIN_URL`` (env.py:120 — the superuser/owner URL).
``modulo_migrate`` is a NOLOGIN role (bootstrap_role.py), so it cannot be
CONNECTED to — the migration executes ``SET ROLE modulo_migrate`` BEFORE
``op.create_table("notification_preferences", ...)``, then ``RESET ROLE``
AFTER (the RLS-enable + policy + grant steps run as the migration's caller).
The post-create ownership assertion verifies the created table's owner is
``modulo_migrate``, not the app role — the owner-bypasses-RLS precondition for
``notification_preferences`` RLS confinement.

The ``modulo_migrate`` role needs CREATE on the public schema + REFERENCES on
the tables its new FKs reference (``organisations``, ``accounts``) —
re-applied idempotently right before ``SET ROLE`` (the pre-alembic bootstrap
runs on a fresh DB where those tables do not exist yet).

The ceremony is conditional on the roles existing (checked via ``pg_roles``):
on a fresh DB where ``alembic upgrade heads`` runs BEFORE the app bootstraps
roles (e.g. the BDD suite), the GRANTs / ``SET ROLE`` / owner assertion are
skipped and the table is created by the migration caller. When the roles exist
(production, where bootstrap runs before alembic), the full ``modulo_migrate``
ownership ceremony runs as described above.

RLS: ENABLE + FORCE ROW LEVEL SECURITY (the owner is ``modulo_migrate`` and
must NOT bypass RLS). Policy split (the read-time model):
  * ``rls_org_isolation``      — org-scope SELECT USING (organisation_id =
    current org) so the notifier's org-only context (set_rls_org, no user
    context) can read opt-outs.
  * ``rls_user_isolation_*``   — per-user INSERT/UPDATE/DELETE constrained to
    organisation_id = current org AND account_id = current app.user_id (the
    value ``set_rls_user_context`` sets — ``app.account_id`` is never set).

``modulo_app`` (the runtime role) is granted full DML. Direct grant, not
default-privileges: the pre-alembic ``bootstrap_role`` run already created the
role, and default privileges only cover tables created afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114_notification_preferences"
down_revision: str | None = "0113_guardrail_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
_USER_SCOPE = (
    "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid "
    "AND account_id = nullif(current_setting('app.user_id', true), '')::uuid"
)


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _assert_owner_is_migrate(bind: sa.Connection) -> None:
    """POST-CREATE ownership assertion — the 0066 ceremony (before RLS)."""
    owner = bind.execute(
        sa.text(
            "SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass('public.notification_preferences')"
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"notification_preferences owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own notification_preferences — owner bypasses RLS)"
        )


def _role_exists(bind: sa.Connection, role: str) -> bool:
    """Return True when the Postgres role exists (fresh dev/BDD DBs have none)."""
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
            op.execute(f"GRANT REFERENCES ON TABLE public.accounts TO {_MIGRATE_ROLE}")
            op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organisation_id",
            "account_id",
            "category",
            name="uq_notification_preferences_org_account_category",
        ),
    )
    op.create_index("ix_notification_preferences_organisation_id", "notification_preferences", ["organisation_id"])

    if pg:
        if migrate_role:
            op.execute("RESET ROLE")
            _assert_owner_is_migrate(bind)
        op.execute("ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE notification_preferences FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON notification_preferences FOR SELECT USING ({_ORG_SCOPE})")
        op.execute(
            f"CREATE POLICY rls_user_isolation_insert ON notification_preferences FOR INSERT WITH CHECK ({_USER_SCOPE})"
        )
        op.execute(
            f"CREATE POLICY rls_user_isolation_update ON notification_preferences "
            f"FOR UPDATE USING ({_USER_SCOPE}) WITH CHECK ({_USER_SCOPE})"
        )
        op.execute(
            f"CREATE POLICY rls_user_isolation_delete ON notification_preferences FOR DELETE USING ({_USER_SCOPE})"
        )
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON notification_preferences TO {_APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("DROP POLICY IF EXISTS rls_user_isolation_delete ON notification_preferences")
        op.execute("DROP POLICY IF EXISTS rls_user_isolation_update ON notification_preferences")
        op.execute("DROP POLICY IF EXISTS rls_user_isolation_insert ON notification_preferences")
        op.execute("DROP POLICY IF EXISTS rls_org_isolation ON notification_preferences")
        op.execute("ALTER TABLE notification_preferences DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_notification_preferences_organisation_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")
