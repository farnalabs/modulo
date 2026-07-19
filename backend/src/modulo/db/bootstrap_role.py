"""Bootstraps the modulo_app runtime role with DML-only permissions.
Connects as the migration/owner user to (re)create the modulo_app role,
then grants SELECT, INSERT, UPDATE, DELETE on all existing and future
tables/sequences, plus USAGE on the public schema.

Safe to run multiple times — checks pg_roles before creating,
and updates the password on each run for consistency.
"""

import asyncio
import os
import sys
from urllib.parse import unquote, urlparse

import asyncpg  # type: ignore[import-untyped]  # asyncpg does not publish a py.typed marker

REQUIRED_VARS = ["DATABASE_ADMIN_URL", "DATABASE_URL"]


def _parse_role(url: str) -> str:
    """Extract the username from a database URL."""
    return urlparse(url).username or ""


async def _bootstrap(admin_url: str, app_url: str) -> None:
    admin_conn_str = admin_url.replace("postgresql+asyncpg://", "postgres://").split("?")[0]
    app_user = _parse_role(app_url)
    parsed = urlparse(app_url)
    app_pass = unquote(parsed.password) if parsed.password else ""

    conn = await asyncpg.connect(admin_conn_str, ssl=False)
    try:
        # Idempotent role creation — skips if already exists
        row = await conn.fetchrow("SELECT 1 FROM pg_roles WHERE rolname = $1", app_user)
        quoted_pass = app_pass.replace("'", "''")
        if not row:
            await conn.execute(f"CREATE ROLE \"{app_user}\" WITH LOGIN PASSWORD '{quoted_pass}' INHERIT")
            print(f"Created role: {app_user}")
        else:
            # Ensure password is up to date
            await conn.execute(f"ALTER ROLE \"{app_user}\" WITH PASSWORD '{quoted_pass}'")
            print(f"Updated password for role: {app_user}")

        # Grant DML on existing tables
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{app_user}"')
        await conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{app_user}"')
        await conn.execute(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{app_user}"')
        # Grant DML on future tables
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_user}"'
        )
        await conn.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{app_user}"')
        print(f"Granted DML permissions to: {app_user}")

    finally:
        await conn.close()


def main() -> None:
    missing = [v for v in REQUIRED_VARS if v not in os.environ]
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    admin_url = os.environ["DATABASE_ADMIN_URL"]
    app_url = os.environ["DATABASE_URL"]

    app_role = _parse_role(app_url)
    print(f"Bootstrapping role: {app_role}")
    print(f"Admin URL host: {admin_url.split('@')[1].split(':')[0] if '@' in admin_url else '?'}")

    try:
        asyncio.run(_bootstrap(admin_url, app_url))
        print("Role bootstrap complete")
    except Exception as exc:
        print(f"ERROR: Role bootstrap failed: [{type(exc).__name__}] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
