"""Bootstraps the runtime DB roles with DML-only permissions.

Connects as the migration/owner user (DATABASE_ADMIN_URL, superuser) to
(re)create the roles, then grants DML on existing and future tables/sequences,
plus USAGE on the public schema. Safe to run multiple times — checks pg_roles
before creating and updates passwords on each run for consistency.

Deliverable (A) of the break-glass admin recovery plan adds:
  * ``modulo_breakglass`` (LOGIN, BYPASSRLS) — the dedicated operator role used
    by the break-glass CLI (dedicated credential from
    MODULO_BREAK_GLASS_DATABASE_URL; a placeholder password is used when the URL
    is not configured yet, replaced on a later boot once (B) ships the URL).
  * ``modulo_migrate`` (NOSUPERUSER, NOLOGIN, BYPASSRLS) — owner of the four
    transferred tables (accounts, org_memberships, token_families,
    org_api_keys) and of the ``deactivate_break_glass`` SECURITY DEFINER so the
    function's cross-org reads/writes work under FORCE RLS.
  * The ``accounts`` UPDATE ALLOW-LIST re-apply (REVOKE table-level UPDATE +
    GRANT the explicit writable columns) — guarded by to_regclass so the
    before-alembic boot run cannot fail on a not-yet-existing table/column.
  * ``modulo_breakglass`` grants per plan §0(c) — SELECT on the read surfaces,
    SELECT+INSERT on accounts/org_memberships, SELECT+INSERT on audit_events,
    SELECT+INSERT/UPDATE on audit_chain_heads, sequence USAGE. The three
    break-glass column UPDATE grants are deliverable (B) and are NOT applied
    here. ``modulo_app`` is NEVER granted membership in either role.
  * Deliverable (B): after the grants are re-applied, the allow-list and role
    posture are ASSERTED (fatal): no table-level UPDATE grant on accounts for
    modulo_app OR PUBLIC, UPDATE-grant set-equality with the allow-list, the
    three break-glass columns not writable by modulo_app, ``rolsuper = false``
    for the app role, ``rolbypassrls = false`` for the app role (tenant
    isolation relies on RLS policies — BYPASSRLS is only for cross-org system
    roles), and no membership in the privileged roles.
"""

import asyncio
import logging
import os
import secrets
import sys
from urllib.parse import unquote, urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]  # asyncpg does not publish a py.typed marker

_log = logging.getLogger(__name__)

REQUIRED_VARS = ["DATABASE_ADMIN_URL", "DATABASE_URL"]

_BREAK_GLASS_ROLE = "modulo_breakglass"
_MIGRATE_ROLE = "modulo_migrate"
_SYSTEM_ROLE = "modulo_system"

# The single-sourced allow-list constant for writable accounts columns.
# Every future column added to accounts must be allow-listed here or be
# read-only (schema-evolution contract — ADR-017/018 amendment).
ACCOUNTS_WRITABLE_COLUMNS = (
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


def _parse_role(url: str) -> str:
    """Extract the username from a database URL."""
    return urlparse(url).username or ""


def _parse_password(url: str) -> str:
    """Extract the password from a database URL, URL-unescaped."""
    parsed = urlparse(url)
    return unquote(parsed.password) if parsed.password else ""


async def _create_or_update_role(
    conn: asyncpg.Connection, name: str, *, login: bool, password: str | None, bypassrls: bool = True
) -> None:
    """Idempotently create/update a role, optionally applying LOGIN/BYPASSRLS.

    ``modulo_app`` must NEVER have BYPASSRLS — RLS policies enforce tenant
    isolation. Only ``modulo_breakglass`` and ``modulo_migrate`` (cross-org
    system roles) receive BYPASSRLS.
    """
    quoted_pass = (password or "").replace("'", "''")
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    if not exists:
        if login:
            if bypassrls:
                await conn.execute(f"CREATE ROLE \"{name}\" LOGIN BYPASSRLS PASSWORD '{quoted_pass}'")
            else:
                await conn.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{quoted_pass}'")
        elif bypassrls:
            await conn.execute(f'CREATE ROLE "{name}" NOSUPERUSER NOLOGIN BYPASSRLS')
        else:
            await conn.execute(f'CREATE ROLE "{name}" NOSUPERUSER NOLOGIN')
        _log.info("Created role: %s (bypassrls=%s)", name, bypassrls)
    else:
        if login:
            if bypassrls:
                await conn.execute(f"ALTER ROLE \"{name}\" WITH LOGIN BYPASSRLS PASSWORD '{quoted_pass}'")
            else:
                await conn.execute(f"ALTER ROLE \"{name}\" WITH LOGIN PASSWORD '{quoted_pass}'")
        elif bypassrls:
            await conn.execute(f'ALTER ROLE "{name}" WITH NOSUPERUSER NOLOGIN BYPASSRLS')
        else:
            await conn.execute(f'ALTER ROLE "{name}" WITH NOSUPERUSER NOLOGIN')
        _log.info("Updated role: %s (bypassrls=%s)", name, bypassrls)


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"))


async def _existing_columns(conn: asyncpg.Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {row["column_name"] for row in rows}


async def _apply_accounts_allow_list(conn: asyncpg.Connection, app_user: str) -> None:
    """Re-apply the accounts UPDATE allow-list for modulo_app.

    Guards on to_regclass + information_schema so the before-alembic boot run
    never fails on a not-yet-existing table/column. The break-glass columns are
    deliberately NOT writable by modulo_app (deliverable (A) posture: only
    direct-DB / SECURITY DEFINER writes can create a break-glass row).
    """
    if not await _table_exists(conn, "accounts"):
        return
    await conn.execute(f'REVOKE UPDATE ON public.accounts FROM "{app_user}"')
    await conn.execute("REVOKE UPDATE ON public.accounts FROM PUBLIC")

    cols = await _existing_columns(conn, "accounts")
    grant_cols = [c for c in ACCOUNTS_WRITABLE_COLUMNS if c in cols]
    if grant_cols:
        await conn.execute(f'GRANT UPDATE ({", ".join(grant_cols)}) ON public.accounts TO "{app_user}"')
        _log.info("Applied accounts UPDATE allow-list for %s: %s", app_user, ", ".join(grant_cols))


async def _grant_break_glass(conn: asyncpg.Connection, bg_user: str) -> None:
    """Grant modulo_breakglass the read/write surfaces it needs (plan §0(c), (A))."""
    select_tables = ("org_memberships", "token_families", "org_api_keys", "organisations")
    existing_select = [t for t in select_tables if await _table_exists(conn, t)]
    if existing_select:
        await conn.execute(f'GRANT SELECT ON {", ".join(f"public.{t}" for t in existing_select)} TO "{bg_user}"')

    for table in ("accounts", "org_memberships"):
        if await _table_exists(conn, table):
            await conn.execute(f'GRANT SELECT, INSERT ON public.{table} TO "{bg_user}"')

    if await _table_exists(conn, "audit_events"):
        await conn.execute(f'GRANT SELECT, INSERT ON public.audit_events TO "{bg_user}"')
    if await _table_exists(conn, "audit_chain_heads"):
        await conn.execute(f'GRANT SELECT, INSERT, UPDATE ON public.audit_chain_heads TO "{bg_user}"')

    await conn.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{bg_user}"')


async def _grant_function_execute(conn: asyncpg.Connection, app_user: str, bg_user: str) -> None:
    """Idempotently re-apply the SECURITY DEFINER EXECUTE grants."""
    func_oid = await conn.fetchval(
        "SELECT to_regprocedure('public.deactivate_break_glass(uuid, uuid, boolean)') IS NOT NULL"
    )
    if func_oid:
        await conn.execute(
            f'GRANT EXECUTE ON FUNCTION public.deactivate_break_glass(uuid, uuid, boolean) TO "{app_user}", "{bg_user}"'
        )


async def _find_allow_list_violations(conn: asyncpg.Connection, app_user: str) -> list[str]:
    """Return a list of allow-list / role-posture violations, or [] when clean.

    Deliverable (B) assertions (plan §0(e)) — the allow-list boundary and role
    posture must survive every boot:
    * no table-level UPDATE grant on ``accounts`` for ``modulo_app`` OR PUBLIC
      (``information_schema.role_table_grants`` is table-level only by
      construction — no ``column_name`` predicate);
    * the set of ``accounts`` columns ``modulo_app`` can UPDATE equals the
      allow-listed writable columns (inverted schema-evolution set-equality),
      and the three break-glass columns are NOT writable by it;
    * ``rolsuper = false`` for the app role;
    * ``rolbypassrls = false`` for the app role (tenant isolation relies on
      RLS policies — BYPASSRLS is only for cross-org system roles);
    * ``modulo_app`` is not a member of ``modulo_breakglass`` / ``modulo_migrate``.

    Runs only when the ``accounts`` table exists — the before-alembic boot run
    on a fresh DB has no tables yet (skip rather than false-pass).
    """
    if not await _table_exists(conn, "accounts"):
        return []

    violations: list[str] = []

    rows = await conn.fetch(
        "SELECT grantee FROM information_schema.role_table_grants "
        "WHERE table_schema = 'public' AND table_name = 'accounts' "
        "AND privilege_type = 'UPDATE' AND grantee IN ($1, 'PUBLIC')",
        app_user,
    )
    if rows:
        violations.append(
            "accounts has a table-level UPDATE grant for: " + ", ".join(sorted(r["grantee"] for r in rows))
        )

    cols = await _existing_columns(conn, "accounts")
    writable = set(ACCOUNTS_WRITABLE_COLUMNS) & cols
    granted: set[str] = set()
    for col in cols:
        ok = await conn.fetchval("SELECT has_column_privilege($1, 'public.accounts', $2, 'UPDATE')", app_user, col)
        if ok:
            granted.add(col)
    if granted != writable:
        violations.append(
            f"modulo_app accounts UPDATE grant drift: granted={sorted(granted)} allow_list={sorted(writable)}"
        )

    for col in ("is_break_glass", "break_glass_expires_at", "break_glass_deactivated_at"):
        if col not in cols:
            continue
        ok = await conn.fetchval("SELECT has_column_privilege($1, 'public.accounts', $2, 'UPDATE')", app_user, col)
        if ok:
            violations.append(f"modulo_app can UPDATE break-glass column {col}")

    if await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = $1", app_user):
        violations.append(f"app role {app_user} is a superuser")

    if await conn.fetchval("SELECT rolbypassrls FROM pg_roles WHERE rolname = $1", app_user):
        violations.append(f"app role {app_user} has BYPASSRLS — tenant isolation relies on RLS policies")

    member_rows = await conn.fetch(
        "SELECT b.rolname FROM pg_auth_members m "
        "JOIN pg_roles a ON a.oid = m.member JOIN pg_roles b ON b.oid = m.roleid "
        "WHERE a.rolname = $1 AND b.rolname IN ($2, $3)",
        app_user,
        _MIGRATE_ROLE,
        _BREAK_GLASS_ROLE,
    )
    if member_rows:
        violations.append(
            f"app role {app_user} is a member of: " + ", ".join(sorted(r["rolname"] for r in member_rows))
        )

    # modulo_system must have BYPASSRLS — it is the dedicated cross-org system
    # cron role. If BYPASSRLS was stripped, system crons silently return zero rows.
    if not await conn.fetchval("SELECT rolbypassrls FROM pg_roles WHERE rolname = $1", _SYSTEM_ROLE):
        violations.append(
            f"modulo_system role {_SYSTEM_ROLE} does not have BYPASSRLS — system crons need cross-org data access"
        )

    return violations


async def _assert_role_posture(conn: asyncpg.Connection, app_user: str) -> None:
    """Fatal when the allow-list / role-posture assertions find a violation."""
    violations = await _find_allow_list_violations(conn, app_user)
    if violations:
        raise RuntimeError("Break-glass role posture assertion FAILED:\n  " + "\n  ".join(violations))


def _asyncpg_admin_connect(admin_url: str) -> tuple[str, bool | str]:
    """Build the asyncpg DSN + ssl arg from the SQLAlchemy admin URL.

    asyncpg.connect() rejects SQLAlchemy-only query params (e.g. ``sslmode``)
    in the DSN, but stripping the query string wholesale drops TLS
    requirements — extract ``sslmode`` and hand it to connect() via ``ssl``.
    """
    dsn = admin_url.replace("postgresql+asyncpg://", "postgres://")
    parts = urlparse(dsn)
    ssl: bool | str = False
    if parts.query:
        for item in parts.query.split("&"):
            key, _, value = item.partition("=")
            if key == "sslmode" and value in {"require", "verify-ca", "verify-full"}:
                ssl = value
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", "", "")), ssl


async def _bootstrap(admin_url: str, app_url: str) -> None:
    admin_conn_str, admin_ssl = _asyncpg_admin_connect(admin_url)
    app_user = _parse_role(app_url)
    app_pass = _parse_password(app_url)

    bg_url = os.environ.get("MODULO_BREAK_GLASS_DATABASE_URL", "")
    bg_user = _parse_role(bg_url) or _BREAK_GLASS_ROLE
    bg_pass = _parse_password(bg_url) or secrets.token_urlsafe(24)

    sys_url = os.environ.get("MODULO_SYSTEM_DATABASE_URL", "")
    sys_user = _parse_role(sys_url) or _SYSTEM_ROLE
    sys_pass = _parse_password(sys_url) or secrets.token_urlsafe(24)

    conn = await asyncpg.connect(admin_conn_str, ssl=admin_ssl)
    try:
        # Idempotent role creation — skips if already exists.
        # modulo_app must NEVER have BYPASSRLS — RLS policies enforce tenant
        # isolation. modulo_system (cross-org system cron role) gets BYPASSRLS.
        await _create_or_update_role(conn, app_user, login=True, password=app_pass, bypassrls=False)

        await _create_or_update_role(conn, _MIGRATE_ROLE, login=False, password=None, bypassrls=True)
        await _create_or_update_role(conn, bg_user, login=True, password=bg_pass, bypassrls=True)
        # modulo_system: dedicated LOGIN BYPASSRLS role for cross-org system cron
        # jobs (analytics_facts_maintenance, journey_reconcile, retention_cleanup,
        # dispatcher_reconcile). Only system crons use this role; modulo_app is
        # NOBYPASSRLS for tenant isolation.
        await _create_or_update_role(conn, sys_user, login=True, password=sys_pass, bypassrls=True)

        # Role grants for schema/DDL ownership (merged from the break-glass
        # 0036 deliverable + the cost 0065 MIGRATE-role deploy-wiring):
        #   - GRANT CREATE ON SCHEMA public: the migration chain (0036
        #     break-glass) transfers ownership of accounts/org_memberships/
        #     token_families/org_api_keys to modulo_migrate via ALTER TABLE
        #     ... OWNER TO, which requires CREATE on the owning schema; and
        #     modulo_migrate creates the RLS-confinement tables (e.g.
        #     cost_components, migration 0066) via SET ROLE modulo_migrate
        #     from the superuser DATABASE_ADMIN_URL. PG15+ does not grant
        #     CREATE to PUBLIC by default, so the explicit grants are
        #     required (hit on the reset staging DB, 2026-08-04).
        #   - GRANT REFERENCES ON organisations: a new table's org FK
        #     references it; guarded by to_regclass because the pre-alembic
        #     bootstrap runs on a fresh DB where organisations does not
        #     exist yet (migration 0066 re-applies both grants itself,
        #     right before SET ROLE).
        await conn.execute(f'GRANT CREATE ON SCHEMA public TO "{_MIGRATE_ROLE}"')
        await conn.execute(f'GRANT CREATE ON SCHEMA public TO "{app_user}"')
        if await _table_exists(conn, "organisations"):
            await conn.execute(f'GRANT REFERENCES ON TABLE public.organisations TO "{_MIGRATE_ROLE}"')

        # Grant DML on existing tables.
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{app_user}"')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{bg_user}"')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{sys_user}"')
        # modulo_migrate owns the SECURITY DEFINER ``lookup_api_key_org``
        # function (0036 transfers ownership) used by API-key auth. The
        # function executes as modulo_migrate, so it needs USAGE on schema
        # public to resolve org_api_keys. Without it, every API-key request
        # fails with ``UndefinedTableError: relation "org_api_keys" does not
        # exist`` on DBs that revoke the PUBLIC default schema USAGE.
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{_MIGRATE_ROLE}"')
        await conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{app_user}"')
        await conn.execute(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{app_user}"')
        # modulo_system: DML on all tables for cross-org system crons.
        await conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{sys_user}"')
        await conn.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{sys_user}"')
        # Grant DML on future tables.
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_user}"'
        )
        await conn.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{app_user}"')
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{sys_user}"'
        )
        await conn.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO "{sys_user}"')

        # Re-apply the accounts UPDATE allow-list (active from deliverable A).
        await _apply_accounts_allow_list(conn, app_user)

        # modulo_breakglass surface grants (A) + function EXECUTE re-apply.
        await _grant_break_glass(conn, bg_user)
        await _grant_function_execute(conn, app_user, bg_user)

        # Deliverable (B): the allow-list + role-posture assertions (fatal).
        await _assert_role_posture(conn, app_user)

        _log.info("Granted DML permissions to: %s", app_user)

    finally:
        await conn.close()


async def bootstrap_roles(admin_url: str, app_url: str) -> None:
    """Public async entry used by the lifespan migration path (before + after alembic)."""
    await _bootstrap(admin_url, app_url)


def main() -> None:
    missing = [v for v in REQUIRED_VARS if v not in os.environ]
    if missing:
        _log.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    admin_url = os.environ["DATABASE_ADMIN_URL"]
    app_url = os.environ["DATABASE_URL"]

    app_role = _parse_role(app_url)
    _log.info("Bootstrapping role: %s", app_role)
    _log.info("Admin URL host: %s", admin_url.split("@")[1].split(":")[0] if "@" in admin_url else "?")

    try:
        asyncio.run(_bootstrap(admin_url, app_url))
        _log.info("Role bootstrap complete")
    except Exception as exc:
        _log.error("Role bootstrap failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
