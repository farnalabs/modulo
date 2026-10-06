"""FAR-1519 item 2 — a warm boot must reconcile the ``modulo_system`` role.

Upgrade-in-place drift, reproduced against a real Postgres:

1. A boot with no ``MODULO_SYSTEM_DATABASE_URL`` creates (or leaves) the
   ``modulo_system`` role with a placeholder password nobody can use.
2. The operator later adds the URL and restarts. Before the fix,
   ``_run_migrations`` returned early on the ``migrations_already_at_head``
   fast-path BEFORE ``_run_bootstrap``, so the warm boot reconciled nothing —
   every system cron failed with ``password authentication failed for user
   "modulo_system"`` until an operator ran ``python -m
   modulo.db.bootstrap_role`` by hand.

Each "boot" here is the real startup step under fix (``main._run_migrations``)
against a DB that is ALREADY at the alembic head — i.e. the warm path, with no
migration and no manual step anywhere. The final assertion is the ticket's:
after supplying the URL, the role authenticates with the URL's password on the
next boot.

State note: the test seeds the drift itself (``ALTER ROLE ... PASSWORD
'placeholder-from-urlless-boot'``) and restores the session baseline (password
``syspass``) by finishing with a URL-configured boot, so a later failure cannot
strand the shared database with a password the rest of the suite's URLs do not
carry.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.conftest import _with_credentials

pytestmark = pytest.mark.integration

_PLACEHOLDER = "placeholder-from-urlless-boot"


def _asyncpg_dsn(url: str) -> str:
    """Convert the suite's SQLAlchemy URL to the DSN asyncpg accepts."""
    return url.replace("postgresql+asyncpg://", "postgres://", 1)


async def _connect(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(_asyncpg_dsn(url))


async def _system_role_hash(admin: asyncpg.Connection) -> str | None:
    """Current scram hash of modulo_system — None only if the role is missing."""
    return await admin.fetchval("SELECT rolpassword FROM pg_authid WHERE rolname = 'modulo_system'")


async def _assert_authenticates_as_system(sys_url: str) -> None:
    """Log in as modulo_system over the configured URL — the ticket's proof."""
    conn = await _connect(sys_url)
    try:
        role = await conn.fetchval("SELECT current_user")
        assert role == "modulo_system", f"expected to authenticate as modulo_system, got {role!r}"
        bypass = await conn.fetchval("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'modulo_system'")
        assert bypass is True, "modulo_system must carry BYPASSRLS for the cross-org system crons"
    finally:
        await conn.close()


async def _dispose_shared_engines() -> None:
    """Dispose and forget the process-global engines (hygiene: this test is
    the first caller of ``get_or_create_engine`` in a focused run, and the
    suite treats stray ResourceWarnings as errors)."""
    from modulo.api import dependencies as api_dependencies
    from modulo.db import session as db_session

    for module, attr in ((api_dependencies, "_engine"), (db_session, "_shared_engine")):
        engine = getattr(module, attr)
        if engine is not None:
            await engine.dispose()
            setattr(module, attr, None)
    api_dependencies._session_factory = None


async def test_warm_boot_reconciles_system_role_password_without_manual_step(
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # admin = the testcontainer superuser; app = modulo_app (the URL the
    # bootstrap treats as the app role); system = the URL an operator supplies
    # at step 3.
    app_url = _with_credentials(migrated_db_url, "modulo_app", "apppass")
    sys_url = _with_credentials(migrated_db_url, "modulo_system", "syspass")

    monkeypatch.setenv("DATABASE_ADMIN_URL", migrated_db_url)
    monkeypatch.delenv("MODULO_BREAK_GLASS_DATABASE_URL", raising=False)
    monkeypatch.delenv("MODULO_SYSTEM_DATABASE_URL", raising=False)

    from modulo.api import main as api_main
    from modulo.settings import get_settings

    settings = get_settings().model_copy(update={"database_url": app_url})

    admin = await _connect(migrated_db_url)
    try:
        # Seed the drift: the role exists but its password matches no
        # configured URL — what a URL-less boot leaves behind.
        await admin.execute(f"ALTER ROLE modulo_system PASSWORD '{_PLACEHOLDER}'")
        stale_hash = await _system_role_hash(admin)
        assert stale_hash, "seed failed: modulo_system must exist with a password"

        # Boot 1 — warm boot (DB at head), system URL still absent: the
        # bootstrap now runs on warm boots, and with no URL it must leave the
        # existing credential alone.
        await api_main._run_migrations(settings)
        hash_after_boot1 = await _system_role_hash(admin)
        assert hash_after_boot1 == stale_hash, "a warm boot with no system URL must not reset the role password"

        # Boot 2 — operator supplies MODULO_SYSTEM_DATABASE_URL and restarts.
        monkeypatch.setenv("MODULO_SYSTEM_DATABASE_URL", sys_url)
        await api_main._run_migrations(settings)
        hash_after_boot2 = await _system_role_hash(admin)
        assert hash_after_boot2 != stale_hash, (
            "the warm boot with a system URL must reconcile the drifted password "
            "(without the fix the fast-path skips the bootstrap and this stays stale)"
        )
        await _assert_authenticates_as_system(sys_url)

        # Boot 3 — another warm boot with the URL configured: still
        # authenticates (the bootstrap is idempotent in password VALUE — the
        # URL's password is re-applied, never a random one).
        await api_main._run_migrations(settings)
        await _assert_authenticates_as_system(sys_url)
    finally:
        await admin.close()
        # Shared-database hygiene: if the test failed between the drift seed
        # and Boot 2, the role would still carry the placeholder password and
        # every later integration test whose URLs say `syspass` would cascade
        # into auth failures. Reconcile back to the session baseline (the
        # same bootstrap the ticket says an operator used to run by hand —
        # here only as cleanup, never as part of the assertions above).
        monkeypatch.setenv("MODULO_SYSTEM_DATABASE_URL", sys_url)
        from modulo.db.bootstrap_role import bootstrap_roles

        await bootstrap_roles(migrated_db_url, app_url)
        await _dispose_shared_engines()
