"""Run at container startup to prepare the database.
1. Fix DATABASE_ADMIN_URL for the SQLAlchemy async driver and fix the runtime
   DATABASE_URL for backwards compatibility
2. Create the alembic_version table with VARCHAR(255) for branch migrations
3. Derive and export MODULO_SYSTEM_DATABASE_URL — if explicitly set it is fixed
   like the other URLs; otherwise it is derived from the fixed DATABASE_URL by
   swapping the username to modulo_system (the password is preserved so
   bootstrap_role.py can create the role). The result is exported to the
   environment and written to /tmp/system_database_url.env.
4. Write the fixed DATABASE_ADMIN_URL / DATABASE_URL / MODULO_SYSTEM_DATABASE_URL
   to files for the shell script
"""

import asyncio
import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg


def _write_env_file(path: str, content: str) -> None:
    """Write *content* to *path* with owner-only (0o600) permissions.

    These files hold database URLs that include credentials, so they must not be
    world-/group-readable even though they live in the world-writable ``/tmp``
    directory (S5443). The consuming shell scripts run as the same user, so the
    restrictive mode does not break them.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


# Strip any sslmode parameter (disable, require, prefer, etc.) — asyncpg
# defaults to "prefer" (try SSL, fall back to plain) on Fly's private
# networks where Postgres doesn't expect SSL, causing ConnectionResetError.
SSLMODE_RE = re.compile(r"[?&]sslmode=[^&]*")


def fix_database_url(url: str) -> str:
    """Convert a Postgres URL to the SQLAlchemy async driver form.

    Replaces ``postgres://`` with ``postgresql+asyncpg://`` and strips any
    ``sslmode`` query parameter (asyncpg defaults to "prefer" on Fly's
    private networks where Postgres doesn't expect SSL, causing
    ConnectionResetError).
    """
    fixed = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return SSLMODE_RE.sub("", fixed).rstrip("?")


def derive_system_database_url(runtime_url: str) -> str:
    """Derive the modulo_system URL from the runtime DATABASE_URL.

    Swaps the username to ``modulo_system``, preserving the password (so
    bootstrap_role.py can create the role with the same credential), the
    host/port, the scheme, and any query params. Returns an empty string
    when the netloc has no ``@`` separator (no userinfo), when the userinfo
    has no password, or when the password is explicitly empty (``user:@host``)
    — in all three cases the caller falls back to modulo_app rather than
    wiring a modulo_system URL whose credential could never match the role
    bootstrap_role.py would create (it seeds ``secrets.token_urlsafe`` when
    the URL carries no password).
    """
    parts = urlsplit(runtime_url)
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    if sep:
        _, _, password = userinfo.partition(":")
        if password:
            new_userinfo = f"modulo_system:{password}"
            parts = parts._replace(netloc=f"{new_userinfo}@{hostport}")
            return urlunsplit(parts)
    return ""


def main() -> None:
    # Step 1: Fix DATABASE_ADMIN_URL and DATABASE_URL
    admin_url = os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL", "")
    original = admin_url
    admin_url = fix_database_url(admin_url)
    os.environ["DATABASE_ADMIN_URL"] = admin_url
    if admin_url != original:
        print("Fixed DATABASE_ADMIN_URL scheme + stripped sslmode")

    # Also fix DATABASE_URL (the runtime URL) for backwards compat
    runtime_url = os.environ.get("DATABASE_URL", "")
    if runtime_url:
        runtime_url = fix_database_url(runtime_url)
        os.environ["DATABASE_URL"] = runtime_url

    # Step 2: Create alembic_version table with VARCHAR(255)
    # Branch migration IDs exceed the default VARCHAR(32).

    async def _bootstrap():
        pg_url = admin_url.replace("postgresql+asyncpg://", "postgres://")
        conn = await asyncpg.connect(pg_url, ssl=False)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS alembic_version (  version_num VARCHAR(255) NOT NULL PRIMARY KEY)"
            )
            print("alembic_version table ready (VARCHAR(255))")
        finally:
            await conn.close()

    try:
        asyncio.run(_bootstrap())
    except Exception as exc:
        print(
            f"WARNING: Could not bootstrap alembic_version: [{type(exc).__name__}] {exc}",
            file=sys.stderr,
        )

    # Step 3: Write the fixed URLs to files for the shell (credentials — 0o600)
    _write_env_file("/tmp/database_url.env", runtime_url)
    _write_env_file("/tmp/database_admin_url.env", admin_url)

    # MODULO_SYSTEM_DATABASE_URL: the modulo_system role (LOGIN, BYPASSRLS) URL used
    # by system crons (metrics_dump, analytics_facts_maintenance, journey_reconcile,
    # retention_cleanup, dispatcher_reconcile) and the SSO pre-auth provider lookup.
    # If set, fix it like the others. If not, derive it from the fixed DATABASE_URL
    # by swapping the username to modulo_system -- the password stays the same, and
    # bootstrap_role.py reads it from the URL to create the role.
    system_url = os.environ.get("MODULO_SYSTEM_DATABASE_URL", "")
    if system_url:
        system_url = fix_database_url(system_url)
    elif runtime_url:
        system_url = derive_system_database_url(runtime_url)
        if not system_url:
            print(
                "WARNING: cannot derive MODULO_SYSTEM_DATABASE_URL "
                "(DATABASE_URL has no usable password/userinfo) "
                "— system crons will fall back to modulo_app",
                file=sys.stderr,
            )

    if system_url:
        os.environ["MODULO_SYSTEM_DATABASE_URL"] = system_url
        # Short-lived container bootstrap env file, consistent with the
        # /tmp/database_url.env and /tmp/database_admin_url.env files written above.
        _write_env_file("/tmp/system_database_url.env", system_url)


if __name__ == "__main__":
    main()
