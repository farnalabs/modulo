"""Bootstraps the modulo_app runtime role with DML-only permissions.
Connects as the migration/owner user to (re)create the modulo_app role,
then grants SELECT, INSERT, UPDATE, DELETE on all existing and future
tables/sequences, plus USAGE on the public schema.

Safe to run multiple times — uses CREATE ROLE ... WITH LOGIN ... INHERIT
wrapped in a DO block so it's idempotent.
"""

import asyncio
import os
import sys

import asyncpg

REQUIRED_VARS = ["DATABASE_ADMIN_URL", "DATABASE_URL"]


def _parse_role(url: str) -> str:
    """Extract the username from a postgres:// or postgresql+asyncpg:// URL."""
    u = url.replace("postgresql+asyncpg://", "postgres://")
    # userinfo is before the @
    userinfo = u.split("@")[0]
    # Remove the scheme
    userinfo = userinfo.split("://")[-1] if "://" in userinfo else userinfo
    return userinfo.split(":")[0]


async def _bootstrap(admin_url: str, app_url: str) -> None:
    admin_conn_str = admin_url.replace("postgresql+asyncpg://", "postgres://")
    app_user = _parse_role(app_url)
    app_pass = app_url.split(":")[2].split("@")[0]  # password between 2nd : and @

    conn = await asyncpg.connect(admin_conn_str, ssl=False)
    try:
        # Idempotent role creation — skips if already exists
        row = await conn.fetchrow("SELECT 1 FROM pg_roles WHERE rolname = $1", app_user)
        if not row:
            await conn.execute(f'CREATE ROLE "{app_user}" WITH LOGIN PASSWORD $1 INHERIT', app_pass)
            print(f"Created role: {app_user}")
        else:
            # Ensure password is up to date
            await conn.execute(f'ALTER ROLE "{app_user}" WITH PASSWORD $1', app_pass)
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
