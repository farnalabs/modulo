"""Integration tests for migration 0176_case_insensitive_emails (FAR-584).

Runs the real Alembic chain against a dedicated Postgres container:

* the fixture upgrades a fresh database to ``0174_per_org_last_admin_guard``
  (the pre-0176 head) and seeds accounts through raw SQL, exactly as a
  pre-upgrade deployment would hold them;
* the tests then run ``upgrade`` to ``0176_case_insensitive_emails`` and
  verify the three contract points: the backfill lowercases every stored
  email, the collision guard FAILS LOUD (RuntimeError listing the colliding
  addresses, no silent merges) when case-insensitive duplicates exist, and
  the case-sensitive ``accounts_email_key`` constraint is replaced by the
  functional unique index ``uq_accounts_email_lower`` which blocks
  case-variant inserts.

Each test gets its own container (function-scoped fixture, same pattern as
``test_migration_0166_uuid_promotion``) so collision-guard failures and
downgrade round-trips never pollute the shared session database.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

_PRE_0176 = "0174_per_org_last_admin_guard"
_REV = "0176_case_insensitive_emails"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _with_credentials(database_url: str, user: str, password: str) -> str:
    from urllib.parse import quote

    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


def _insert_account_sql(email: str) -> tuple[str, dict[str, str]]:
    return (
        "INSERT INTO public.accounts (id, email, display_name) VALUES (:id, :email, :display_name)",
        {"id": str(uuid.uuid4()), "email": email, "display_name": "Seed User"},
    )


@pytest.fixture
def fresh_0174_db(monkeypatch):
    """A fresh DB upgraded to the pre-0176 head (``0174``), for 0176 tests.

    Spins up its own Postgres container, provisions the migration roles,
    runs ``alembic upgrade`` to ``0174_per_org_last_admin_guard``, and tears
    the container down afterwards. ``DATABASE_URL`` is redirected to this
    ephemeral container for the duration (same monkeypatch.context pattern
    as ``test_migration_0166_uuid_promotion``): consuming tests MUST NOT
    re-set ``DATABASE_URL`` themselves — they read the URL from this
    fixture's return value only.
    """
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    raw = pg.get_connection_url().replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")

    def asyncio_run(coro):
        import asyncio

        return asyncio.run(coro)

    async def _provision():
        eng = create_async_engine(raw)
        async with eng.connect() as conn:
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_migrate"'))
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_breakglass"'))
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_app"'))
            await conn.execute(text("CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
            await conn.execute(text("CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
            await conn.execute(text("CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'"))
            await conn.commit()
        await eng.dispose()

    asyncio_run(_provision())

    app_url = _with_credentials(raw, "modulo_app", "apppass")
    bg_url = _with_credentials(raw, "modulo_breakglass", "bgpass")
    config = _alembic_config(raw)
    with monkeypatch.context() as m:
        m.setenv("DATABASE_URL", raw)
        m.setenv("DATABASE_ADMIN_URL", raw)
        m.setenv("MODULO_BREAK_GLASS_DATABASE_URL", bg_url)
        from modulo.db.bootstrap_role import bootstrap_roles

        asyncio_run(bootstrap_roles(raw, app_url))
        command.upgrade(config, _PRE_0176)
        asyncio_run(bootstrap_roles(raw, app_url))
        yield raw
    pg.stop()


async def _upgrade_to_0176(db_url: str) -> None:
    command.upgrade(_alembic_config(db_url), _REV)


async def _seed_accounts(db_url: str, emails: list[str]) -> None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for email in emails:
                stmt, params = _insert_account_sql(email)
                await conn.execute(text(stmt), params)
    finally:
        await engine.dispose()


async def test_0176_backfills_emails_and_swaps_index(fresh_0174_db: str) -> None:
    db_url = fresh_0174_db
    await _seed_accounts(db_url, ["  Mixed@Case.COM ", "Second@Example.ORG"])
    await _upgrade_to_0176(db_url)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            emails = (await conn.execute(text("SELECT email FROM public.accounts ORDER BY email"))).scalars().all()
            index_names = (
                (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'accounts'")
                    )
                )
                .scalars()
                .all()
            )
            constraint_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = (SELECT oid FROM pg_class WHERE relname = 'accounts')"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert set(emails) == {"mixed@case.com", "second@example.org"}, f"backfill did not canonicalise: {emails}"
    assert "uq_accounts_email_lower" in index_names, f"functional unique index missing: {sorted(index_names)}"
    assert "accounts_email_key" not in constraint_names, (
        f"case-sensitive unique constraint must be dropped: {sorted(constraint_names)}"
    )


async def test_0176_collision_guard_fails_loud(fresh_0174_db: str) -> None:
    db_url = fresh_0174_db
    # Case-sensitive uniqueness at 0174 admits both rows; 0176 must refuse
    # the upgrade instead of silently merging the accounts.
    await _seed_accounts(db_url, ["Alice@Example.com", "ALICE@example.com"])

    from alembic.util import CommandError

    with pytest.raises((RuntimeError, CommandError)) as exc_info:
        await _upgrade_to_0176(db_url)

    message = str(exc_info.value)
    assert "case-insensitively" in message, message
    assert "alice@example.com" in message, message

    # The guard fired BEFORE the index swap: the case-sensitive constraint
    # is still in place and no functional index was created.
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            constraint_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = (SELECT oid FROM pg_class WHERE relname = 'accounts')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            index_names = (
                (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'accounts'")
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert "accounts_email_key" in constraint_names
    assert "uq_accounts_email_lower" not in index_names


async def test_0176_unique_index_blocks_case_variant_insert(fresh_0174_db: str) -> None:
    db_url = fresh_0174_db
    await _upgrade_to_0176(db_url)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        stmt, params = _insert_account_sql("User@Case.com")
        async with engine.begin() as conn:
            await conn.execute(text(stmt), params)

        duplicate_stmt, duplicate_params = _insert_account_sql("user@case.com")
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(duplicate_stmt), duplicate_params)
    finally:
        await engine.dispose()


async def test_0176_crud_lookup_matches_case_variants_post_upgrade(fresh_0174_db: str) -> None:
    """The CRUD lookup resolves a case-variant even for a row stored
    mixed-case (pre-backfill spellings written by an older deployment)."""
    db_url = fresh_0174_db
    await _seed_accounts(db_url, ["Mixed@Case.COM"])
    await _upgrade_to_0176(db_url)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        from modulo.db.crud.account import get_account_by_email

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            found = await get_account_by_email(session, "mixed@case.com")
    finally:
        await engine.dispose()

    assert found is not None, "case-insensitive lookup must resolve a case-variant of the stored email"


async def test_0176_downgrade_restores_case_sensitive_constraint(fresh_0174_db: str) -> None:
    import types

    db_url = fresh_0174_db
    await _seed_accounts(db_url, ["Keep@Case.com"])
    await _upgrade_to_0176(db_url)

    config = _alembic_config(db_url)
    # env.py's boot fast-path skips programmatic alembic invocations when the
    # DB is at head (config.cmd_opts is None -> _invocation_is_upgrade()
    # wrongly True). Force the downgrade direction so it actually runs — same
    # pattern as test_migration_0126_eval_suite.py.
    config.cmd_opts = types.SimpleNamespace(command="downgrade")
    command.downgrade(config, _PRE_0176)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            constraint_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = (SELECT oid FROM pg_class WHERE relname = 'accounts')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            index_names = (
                (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'accounts'")
                    )
                )
                .scalars()
                .all()
            )
            emails = (await conn.execute(text("SELECT email FROM public.accounts"))).scalars().all()
    finally:
        await engine.dispose()

    assert "accounts_email_key" in constraint_names, (
        f"downgrade must restore the constraint: {sorted(constraint_names)}"
    )
    assert "uq_accounts_email_lower" not in index_names
    # The backfill is deliberately NOT reversed: the lowercased spelling stays.
    assert emails == ["keep@case.com"], f"downgrade must not resurrect mixed-case spellings: {emails}"
