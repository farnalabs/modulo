"""Break-glass enforcement: accounts UPDATE allow-list + modulo_breakglass grants (deliverable B).

Revision ID: 0037_break_glass_enforcement
Revises: 0036_break_glass_columns
Create Date: 2026-08-03

Per docs/break-glass-admin-recovery-plan.md v17 (deliverable B):

1. The accounts UPDATE ALLOW-LIST is asserted in DDL: table-level UPDATE is
   revoked from ``modulo_app`` AND ``PUBLIC``, and UPDATE is granted on the ten
   explicit writable columns to ``modulo_app``. The ten-column list is frozen
   here as a migration snapshot; the single-sourced constant lives in
   ``modulo.db.bootstrap_role.ACCOUNTS_WRITABLE_COLUMNS`` and the inverted
   schema-evolution integration test asserts set-equality so the two cannot
   drift. Bootstrap re-applies the allow-list on every boot regardless.
2. ``modulo_breakglass`` receives the plan §0(c) surface: SELECT on the read
   tables (org_memberships, token_families, org_api_keys, organisations);
   SELECT + INSERT on accounts and org_memberships; SELECT + INSERT on
   audit_events; SELECT + INSERT/UPDATE on audit_chain_heads; sequence USAGE;
   and UPDATE on the THREE break-glass columns ONLY (is_break_glass,
   break_glass_expires_at, break_glass_deactivated_at). NOT DELETE; no other
   UPDATE. The column-grant DDL is guarded by nothing further here because the
   three columns are added by 0036 (this migration is chained directly after
   it) and the roles are provisioned by bootstrap before alembic runs.
3. ALTER DEFAULT PRIVILEGES: there is no ``modulo_migrate`` default-privileges
   item to drop (grep-verified — bootstrap only sets the app-role defaults);
   this migration adds none. The four transferred tables are owned by
   ``modulo_migrate`` with explicit grants (0036).
4. The rolsuper = false / no-membership-in-privileged-roles / allow-list-survived
   boot assertions live in ``bootstrap_role.py`` — this migration is DDL only.

Downgrade order (pinned): drop ``modulo_breakglass``'s column grants and
re-GRANT table-level UPDATE to ``modulo_app`` BEFORE 0036's downgrade runs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037_break_glass_enforcement"
down_revision: str | Sequence[str] | None = "0036_break_glass_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNTS_WRITABLE_COLUMNS = (
    "email",
    "display_name",
    "password_hash",
    "active",
    "auth_provider",
    "sso_subject",
    "preferences",
    "last_login",
    "is_system_admin",
    "updated_at",
)

_BREAK_GLASS_COLUMNS = ("is_break_glass", "break_glass_expires_at", "break_glass_deactivated_at")


def upgrade() -> None:
    # 1. Accounts UPDATE allow-list — table-level UPDATE revoked, explicit
    #    writable columns granted to modulo_app (frozen snapshot of the
    #    single-sourced bootstrap_role.ACCOUNTS_WRITABLE_COLUMNS constant).
    op.execute("REVOKE UPDATE ON public.accounts FROM modulo_app")
    op.execute("REVOKE UPDATE ON public.accounts FROM PUBLIC")
    op.execute(
        "GRANT UPDATE (email, display_name, password_hash, active, auth_provider, "
        "sso_subject, preferences, last_login, is_system_admin, updated_at) "
        "ON public.accounts TO modulo_app"
    )

    # 2. modulo_breakglass surface (plan §0(c)) — the three break-glass column
    #    UPDATE grants are the deliverable-(B) addition.
    op.execute(
        "GRANT UPDATE (is_break_glass, break_glass_expires_at, break_glass_deactivated_at) "
        "ON public.accounts TO modulo_breakglass"
    )
    op.execute(
        "GRANT SELECT ON public.org_memberships, public.token_families, "
        "public.org_api_keys, public.organisations TO modulo_breakglass"
    )
    op.execute("GRANT SELECT, INSERT ON public.accounts, public.org_memberships TO modulo_breakglass")
    op.execute("GRANT SELECT, INSERT ON public.audit_events TO modulo_breakglass")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.audit_chain_heads TO modulo_breakglass")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO modulo_breakglass")


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (is_break_glass, break_glass_expires_at, break_glass_deactivated_at) ON public.accounts FROM modulo_breakglass"
    )
    op.execute(
        "REVOKE UPDATE (email, display_name, password_hash, active, auth_provider, "
        "sso_subject, preferences, last_login, is_system_admin, updated_at) "
        "ON public.accounts FROM modulo_app"
    )
    # Restore the pre-0037 posture: modulo_app gets table-level UPDATE back
    # before 0036's downgrade drops the break-glass columns.
    op.execute("GRANT UPDATE ON public.accounts TO modulo_app")
