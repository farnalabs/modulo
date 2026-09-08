"""Run at container startup AND from the native launcher to prepare the database.

Promoted from ``deploy/fly/bootstrap_db.py`` (FAR-671 / ADR 031 Decision 2):
the deploy path now imports this module through a thin shim so the container
and native boots share the exact logic and cannot drift. Dependency-light by
contract - importable WITHOUT SQLAlchemy/models; asyncpg-level only.

1. Fix DATABASE_ADMIN_URL for the SQLAlchemy async driver and fix the runtime
   DATABASE_URL for backwards compatibility
2. Create the alembic_version table with VARCHAR(255) for branch migrations
3. Derive and export MODULO_SYSTEM_DATABASE_URL - if explicitly set it is fixed
   like the other URLs; otherwise it is derived from the fixed DATABASE_URL by
   swapping the username to modulo_system (the password is preserved so
   bootstrap_role.py can create the role). The result is exported to the
   environment and written to /tmp/system_database_url.env.
4. Write the fixed DATABASE_ADMIN_URL / DATABASE_URL / MODULO_SYSTEM_DATABASE_URL
   to files for the shell script
"""

import asyncio
import os
import secrets as _secrets
import sys
from pathlib import Path

import asyncpg

from modulo.db.url_utils import derive_system_database_url, fix_database_url

__all__ = ["main"]

_TMP_NAME_ATTEMPTS = 8


def _write_env_file(path: str, content: str) -> None:
    """Write *content* to *path* with owner-only (0o600) permissions.

    These files hold database URLs that include credentials, so they must not be
    world-/group-readable even though they live in the world-writable ``/tmp``
    directory (S5443). The consuming shell scripts run as the same user, so the
    restrictive mode does not break them.

    The fixed *path* is a predictable name in a world-writable directory, so a
    plain create/truncate would let an attacker pre-create (or symlink) it and
    read the credentials. Instead the content is written to a RANDOM-named
    sibling created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` (a pre-created or
    symlinked temp name can never be adopted or followed), fsynced, and then
    atomically renamed over the contract path — the renamed inode carries the
    0600 mode and replaces whatever (file or symlink) squatted there. A symlink
    on the contract path is rejected loudly rather than followed.
    """
    target = Path(path)
    last_error: OSError | None = None
    for _ in range(_TMP_NAME_ATTEMPTS):
        tmp_path = target.parent / f"{target.name}.tmp-{_secrets.token_hex(8)}"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError as exc:
            last_error = exc  # virtually impossible random-name collision; retry
            continue
        try:
            os.write(fd, content.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        if target.is_symlink():
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"refusing to write {path}: a symlink squats on the contract path")
        tmp_path.replace(target)
        return
    raise RuntimeError(f"could not secure a private temp file next to {path}") from last_error


def main() -> None:
    # Step 1: Fix DATABASE_ADMIN_URL and DATABASE_URL
    admin_url = os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL", "")
    original = admin_url
    admin_url = fix_database_url(admin_url)
    os.environ["DATABASE_ADMIN_URL"] = admin_url
    if admin_url != original:
        print("Fixed DATABASE_ADMIN_URL scheme + stripped sslmode")  # noqa: T201

    # Also fix DATABASE_URL (the runtime URL) for backwards compat
    runtime_url = os.environ.get("DATABASE_URL", "")
    if runtime_url:
        runtime_url = fix_database_url(runtime_url)
        os.environ["DATABASE_URL"] = runtime_url

    # Step 2: Create alembic_version table with VARCHAR(255)
    # Branch migration IDs exceed the default VARCHAR(32).

    async def _bootstrap() -> None:
        pg_url = admin_url.replace("postgresql+asyncpg://", "postgres://")
        conn = await asyncpg.connect(pg_url, ssl=False)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS alembic_version (  version_num VARCHAR(255) NOT NULL PRIMARY KEY)"
            )
            print("alembic_version table ready (VARCHAR(255))")  # noqa: T201
        finally:
            await conn.close()

    try:
        asyncio.run(_bootstrap())
    except Exception as exc:
        print(  # noqa: T201
            f"WARNING: Could not bootstrap alembic_version: [{type(exc).__name__}] {exc}",
            file=sys.stderr,
        )

    # Step 3: Write the fixed URLs to files for the shell (credentials - 0o600)
    # The /tmp paths are the container-entrypoint contract (consumed by the
    # shell scripts). The predictable-name hazards of a world-writable /tmp
    # are mitigated inside _write_env_file: exclusive-create random temp
    # sibling (O_CREAT|O_EXCL|O_NOFOLLOW, 0600) atomically renamed over the
    # contract path, symlinked contract paths rejected. The /tmp prefix here
    # is the deployment contract, not an unchecked temp usage.
    # /tmp is the container-entrypoint contract (consumed by the shell scripts); the
    # world-writable-dir hazard is mitigated inside _write_env_file via
    # O_CREAT|O_EXCL|O_NOFOLLOW + 0o600 + atomic rename over the contract path +
    # symlinked-contract-path rejection, so the credential files cannot be pre-created
    # or symlink-hijacked by another /tmp writer. NOSONAR below suppresses S5443.
    _write_env_file("/tmp/database_url.env", runtime_url)  # nosec B108  # noqa: S108  # NOSONAR S5443
    _write_env_file("/tmp/database_admin_url.env", admin_url)  # nosec B108  # noqa: S108  # NOSONAR S5443

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
            print(  # noqa: T201
                "WARNING: cannot derive MODULO_SYSTEM_DATABASE_URL "
                "(DATABASE_URL has no usable password/userinfo) "
                "- system crons will fall back to modulo_app",
                file=sys.stderr,
            )

    if system_url:
        os.environ["MODULO_SYSTEM_DATABASE_URL"] = system_url
        # Short-lived container bootstrap env file, consistent with the
        # /tmp/database_url.env and /tmp/database_admin_url.env files written above.
        _write_env_file("/tmp/system_database_url.env", system_url)  # nosec B108  # noqa: S108  # NOSONAR S5443
