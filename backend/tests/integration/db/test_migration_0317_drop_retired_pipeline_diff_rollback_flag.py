"""Integration test for migration 0316_drop_retired_pipeline_diff_rollback_flag (FAR-1875).

Against REAL Postgres (testcontainers; the private-database harness from
``test_migration_0315_service_accounts_lifecycle.py`` /
``test_migration_0262_hitl_gate_to_review_vocabulary.py``) — a substring-routed
mock proves nothing about SQL semantics. FAR-1819 retired the
``pipeline_diff_rollback`` tier gate but the boot seeder only upserts
(``INSERT ... ON CONFLICT (name) DO NOTHING``), so already-seeded databases
(staging, production) keep a stale ``feature_flag_catalog`` row that the
DB-backed admin Feature Flags listing still surfaces. This test proves, on real
Postgres:

* the REAL alembic ``upgrade`` of 0316 DELETES that stale row while leaving a
  sibling flag row untouched, and the DB-backed registry
  (``FeatureFlagRegistry.from_db`` -> ``_load_catalog`` -> ``list_feature_flags``,
  the listing ``admin_feature_flags.list_feature_flags`` builds) STOPS
  reporting it — the actual admin-UI symptom;
* re-executing the migration's delete SQL is a no-op (idempotent);
* on a database where the row was never seeded the upgrade completes with no
  error and leaves the catalog table intact;
* on a database where the episode table itself is absent the guarded SQL
  (delete AND restore) is a no-op rather than an error;
* ``downgrade`` restores the retired row (the pre-FAR-1819 definition).

The ~316-migration chain is applied ONCE to a private template database and
each test clones it (``CREATE DATABASE ... WITH TEMPLATE``), so every test still
runs the real migration without paying the whole-chain cost five times.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.feature_flags import FeatureFlagRegistry

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parents[3]

MIGRATION_REV = "0317_drop_retired_pipeline_diff_rollback_flag"
PREV_REV = "0316_policy_gate_evaluation_mode"
RETIRED_FLAG = "pipeline_diff_rollback"
SIBLING_FLAG = "email_config"

_MIGRATION_PATH = BACKEND_ROOT / "src" / "modulo" / "db" / "migrations" / "versions" / f"{MIGRATION_REV}.py"


def _migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{MIGRATION_REV}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic_config(db_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    admin = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"
            ).bindparams(n=db_name)
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin.dispose()


def _run_alembic(iso_url: str, cmd: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        cfg = _alembic_config(iso_url)
        assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
        getattr(command, cmd)(cfg, target)


@pytest_asyncio.fixture(scope="session")
async def _prev_rev_template(db_url: str) -> AsyncGenerator[str, None]:
    """A private Postgres TEMPLATE database migrated once to PREV_REV (0315)."""
    db_name, iso_url = await _fresh_db(db_url, "m0316_tmpl")
    _run_alembic(iso_url, "upgrade", PREV_REV)
    admin = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin.connect() as conn:
        await conn.execute(text(f'ALTER DATABASE "{db_name}" IS_TEMPLATE true'))
    await admin.dispose()
    try:
        yield db_name
    finally:
        # A template database cannot be dropped until the marker is cleared.
        admin = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
        async with admin.connect() as conn:
            await conn.execute(text(f'ALTER DATABASE "{db_name}" IS_TEMPLATE false'))
        await admin.dispose()
        await _drop_db(db_url, db_name)


@pytest_asyncio.fixture
async def prev_rev_db_url(db_url: str, _prev_rev_template: str) -> AsyncGenerator[str, None]:
    """A per-test clone of the PREV_REV template, so no test observes another's writes."""
    db_name = f"m0316_t_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE "{_prev_rev_template}"'))
    await admin.dispose()
    try:
        yield _swap_db_name(db_url, db_name)
    finally:
        await _drop_db(db_url, db_name)


async def _seed_catalog(iso_url: str, flags: tuple[str, ...]) -> None:
    """Seed the ``team`` tier (the FK target) and the given flag rows."""
    engine = create_async_engine(iso_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description) "
                    "VALUES ('team', 'Team', 1, true, 'Paid tier') ON CONFLICT (tier_id) DO NOTHING"
                )
            )
            for name in flags:
                await conn.execute(
                    text(
                        "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
                        "VALUES (:name, :name, 'team', NULL, true) ON CONFLICT (name) DO NOTHING"
                    ),
                    {"name": name},
                )
    finally:
        await engine.dispose()


async def _table_flag_names(iso_url: str) -> set[str]:
    engine = create_async_engine(iso_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT name FROM feature_flag_catalog"))).scalars().all()
        return set(rows)
    finally:
        await engine.dispose()


async def _registry_flag_names(iso_url: str) -> set[str]:
    """Flag names the DB-backed registry reports — the admin listing's source."""
    engine = create_async_engine(iso_url, poolclass=NullPool)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            registry = await FeatureFlagRegistry.from_db(session, current_tier="community")
            return {flag.name for flag in registry.list_flags()}
    finally:
        await engine.dispose()


async def test_upgrade_deletes_only_the_retired_flag_row(prev_rev_db_url: str) -> None:
    await _seed_catalog(prev_rev_db_url, flags=(RETIRED_FLAG, SIBLING_FLAG))

    # Prove the fix is meaningful: before the migration, the DB-backed registry
    # (the source of admin_feature_flags.list_feature_flags) REPORTS the flag —
    # so an assertion it is gone afterwards would fail without this change.
    before = await _registry_flag_names(prev_rev_db_url)
    assert RETIRED_FLAG in before
    assert SIBLING_FLAG in before

    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)

    table_names = await _table_flag_names(prev_rev_db_url)
    assert RETIRED_FLAG not in table_names
    assert SIBLING_FLAG in table_names

    registry_names = await _registry_flag_names(prev_rev_db_url)
    assert RETIRED_FLAG not in registry_names
    assert SIBLING_FLAG in registry_names


async def test_repeated_migration_sql_is_idempotent(prev_rev_db_url: str) -> None:
    await _seed_catalog(prev_rev_db_url, flags=(RETIRED_FLAG,))
    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)

    # Re-run the migration's OWN delete statement twice; neither may raise and
    # the row must stay gone (the guard matches nothing on an empty/exhausted
    # catalog).
    engine = create_async_engine(prev_rev_db_url, poolclass=NullPool)
    try:
        delete_sql = _migration_module()._DELETE_RETIRED_FLAG_SQL
        async with engine.begin() as conn:
            await conn.execute(text(delete_sql))
            await conn.execute(text(delete_sql))
    finally:
        await engine.dispose()

    assert RETIRED_FLAG not in await _table_flag_names(prev_rev_db_url)


async def test_upgrade_is_noop_when_retired_row_absent(prev_rev_db_url: str) -> None:
    # A database migrated past the seed point but where the retired flag was
    # never seeded (a fresh install from source) must upgrade cleanly.
    await _seed_catalog(prev_rev_db_url, flags=(SIBLING_FLAG,))

    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)

    table_names = await _table_flag_names(prev_rev_db_url)
    assert SIBLING_FLAG in table_names
    assert RETIRED_FLAG not in table_names


async def test_guarded_sql_is_noop_when_table_absent(db_url: str) -> None:
    db_name, iso_url = await _fresh_db(db_url, "m0316_notable")
    try:
        engine = create_async_engine(iso_url, poolclass=NullPool)
        try:
            module = _migration_module()
            async with engine.begin() as conn:
                # No ``feature_flag_catalog`` table exists yet — both directions
                # must short-circuit on the information_schema guard.
                await conn.execute(text(module._DELETE_RETIRED_FLAG_SQL))
                await conn.execute(text(module._RESTORE_RETIRED_FLAG_SQL))
                catalog_oid = (
                    await conn.execute(text("SELECT to_regclass('public.feature_flag_catalog')"))
                ).scalar_one()
        finally:
            await engine.dispose()
        assert catalog_oid is None
    finally:
        await _drop_db(db_url, db_name)


async def test_downgrade_restores_the_retired_row(prev_rev_db_url: str) -> None:
    await _seed_catalog(prev_rev_db_url, flags=(RETIRED_FLAG,))
    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)
    assert RETIRED_FLAG not in await _table_flag_names(prev_rev_db_url)

    _run_alembic(prev_rev_db_url, "downgrade", PREV_REV)

    assert RETIRED_FLAG in await _table_flag_names(prev_rev_db_url)
