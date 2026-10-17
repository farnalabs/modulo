"""Integration test for migration 0317_drop_retired_pipeline_diff_rollback_flag (FAR-1875).

Against REAL Postgres (testcontainers; the private-database harness from
``test_migration_0315_service_accounts_lifecycle.py`` /
``test_migration_0262_hitl_gate_to_review_vocabulary.py``) — a substring-routed
mock proves nothing about SQL semantics. The migration deletes the
``feature_flag_catalog`` row for every flag retired from the code registry
(the revision is still named for ``pipeline_diff_rollback``, the FAR-1819
retirement that prompted the sweep). Because the boot seeder only upserts
(``INSERT ... ON CONFLICT (name) DO NOTHING``), already-seeded databases
(staging, production) keep a stale row for every flag ever removed from
``_KNOWN_FLAGS`` that the DB-backed admin Feature Flags listing still surfaces.
This test proves, on real Postgres:

* the REAL alembic ``upgrade`` of 0317 DELETES the retired ``pipeline_diff_rollback``
  row while leaving a live sibling flag row untouched, and the DB-backed registry
  (``FeatureFlagRegistry.from_db`` -> ``_load_catalog`` -> ``list_feature_flags``,
  the listing ``admin_feature_flags.list_feature_flags`` builds) STOPS
  reporting it — the actual admin-UI symptom;
* re-running the REAL migration ``upgrade()`` a second time is a no-op (idempotent);
* the sweep is COMPLETE, not an enumerate-only list: a sentinel row whose name is
  in NEITHER the live registry NOR the retired-name list is removed by the real
  ``upgrade()`` (the old enumerate-only migration left it behind), and every
  remaining row is a live code flag;
* on a database where the row was never seeded the upgrade completes with no
  error and leaves the catalog table intact;
* on a database where the catalog table (or its ``name`` column) is absent, or
  where ``tier_catalog`` is absent, the guarded SQL (delete AND restore) is a
  no-op rather than an error, and pre-existing state is left unchanged;
* ``downgrade`` restores the retired rows.

The ~317-migration chain is applied ONCE to a private template database and
each test clones it (``CREATE DATABASE ... WITH TEMPLATE``), so every test still
runs the real migration without paying the whole-chain cost on every test.
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

from modulo.core.feature_flags import _KNOWN_FLAGS, FeatureFlagRegistry
from modulo.core.seed_data.catalog import FLAGS

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parents[3]

MIGRATION_REV = "0317_drop_retired_pipeline_diff_rollback_flag"
PREV_REV = "0316_policy_gate_evaluation_mode"
RETIRED_FLAG = "pipeline_diff_rollback"
SIBLING_FLAG = "email_config"

#: A name in NEITHER the live registry NOR the retired-name list. Only a sweep
#: that DERIVES its stale set (rather than enumerating retired names) removes
#: it — the assertion that makes the completeness test non-tautological.
UNKNOWN_SENTINEL = "zzz_far1875_unknown_sentinel"

#: The live code registry — the invariant source of truth for the structural test.
LIVE_FLAG_NAMES: frozenset[str] = frozenset(str(flag["name"]) for flag in FLAGS)

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
    """A private Postgres TEMPLATE database migrated once to PREV_REV
    (``0316_policy_gate_evaluation_mode``)."""
    db_name, iso_url = await _fresh_db(db_url, "m0317_tmpl")
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
    db_name = f"m0317_t_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE "{_prev_rev_template}"'))
    await admin.dispose()
    try:
        yield _swap_db_name(db_url, db_name)
    finally:
        await _drop_db(db_url, db_name)


async def _seed_catalog(iso_url: str, flags: tuple[str, ...]) -> None:
    """Seed both tiers (the FK targets the retired rows reference) and the given flag rows."""
    engine = create_async_engine(iso_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description) VALUES "
                    "('community', 'Community', 0, false, 'Free tier'), "
                    "('team', 'Team', 1, true, 'Paid tier') ON CONFLICT (tier_id) DO NOTHING"
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


async def _set_alembic_version(iso_url: str, revision: str) -> None:
    """Rewind the version marker so a subsequent ``upgrade`` re-runs the migration."""
    engine = create_async_engine(iso_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = :rev"), {"rev": revision})
    finally:
        await engine.dispose()


def test_retired_flag_names_are_absent_from_the_live_registry() -> None:
    """The sweep's safety precondition: no swept name may be a live code flag.

    Deleting a catalog row for a name still in ``_KNOWN_FLAGS`` would gate that
    feature off (``get_flag`` -> ``None`` -> ``feature_enabled`` is ``False``).
    """
    live = {flag.name for flag in _KNOWN_FLAGS}
    overlap = live & set(_migration_module().RETIRED_FLAG_NAMES)
    assert not overlap, f"live flags must never be swept: {sorted(overlap)}"


async def test_upgrade_deletes_the_retired_flag_row(prev_rev_db_url: str) -> None:
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


async def test_after_upgrade_every_catalog_row_is_a_live_code_flag(prev_rev_db_url: str) -> None:
    """Structural invariant: the catalog may only contain live code-flag names.

    Non-tautological: the sentinel is named by NEITHER the retired list NOR the
    live registry, so an enumerate-only sweep leaves it behind. Only a sweep
    that derives its stale set from the (frozen) registry snapshot removes it.
    """
    module = _migration_module()
    seeded = tuple(sorted(LIVE_FLAG_NAMES | set(module.RETIRED_FLAG_NAMES) | {UNKNOWN_SENTINEL}))
    await _seed_catalog(prev_rev_db_url, flags=seeded)

    before = await _table_flag_names(prev_rev_db_url)
    assert set(module.RETIRED_FLAG_NAMES) <= before
    assert UNKNOWN_SENTINEL in before

    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)

    remaining = await _table_flag_names(prev_rev_db_url)

    # Completeness: the sentinel was never enumerated anywhere, yet is gone.
    assert UNKNOWN_SENTINEL not in remaining
    # The structural invariant the admin listing depends on: no row survives
    # whose name the current code registry does not know.
    assert remaining <= LIVE_FLAG_NAMES
    # The sweep must not over-delete: every name in the frozen registry snapshot
    # that was seeded survives (this catches a delete predicate that eats a live
    # flag). Together with ``remaining <= LIVE_FLAG_NAMES`` this pins the result
    # without re-deriving it, so a future registry addition cannot make the test
    # tautologically re-assert the migration's own input.
    assert set(module.LIVE_FLAG_NAMES_SNAPSHOT) <= remaining


async def test_repeated_migration_upgrade_is_idempotent(prev_rev_db_url: str) -> None:
    await _seed_catalog(prev_rev_db_url, flags=(RETIRED_FLAG, SIBLING_FLAG))
    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)
    assert RETIRED_FLAG not in await _table_flag_names(prev_rev_db_url)

    # Rewind the version marker so the REAL migration ``upgrade()`` runs a
    # SECOND time against a catalog where the row is already gone; it must
    # neither raise nor disturb a live sibling row.
    await _set_alembic_version(prev_rev_db_url, PREV_REV)
    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)

    names = await _table_flag_names(prev_rev_db_url)
    assert RETIRED_FLAG not in names
    assert SIBLING_FLAG in names


async def test_upgrade_is_noop_when_retired_row_absent(prev_rev_db_url: str) -> None:
    # A database migrated past the seed point but where the retired flag was
    # never seeded (a fresh install from source) must upgrade cleanly.
    await _seed_catalog(prev_rev_db_url, flags=(SIBLING_FLAG,))

    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)

    table_names = await _table_flag_names(prev_rev_db_url)
    assert SIBLING_FLAG in table_names
    assert RETIRED_FLAG not in table_names


async def test_guarded_sql_is_noop_when_catalog_table_absent(db_url: str) -> None:
    db_name, iso_url = await _fresh_db(db_url, "m0317_notable")
    try:
        engine = create_async_engine(iso_url, poolclass=NullPool)
        try:
            module = _migration_module()
            async with engine.begin() as conn:
                # A pre-existing, unrelated row the guarded SQL must not touch,
                # plus no ``feature_flag_catalog`` table at all.
                await conn.execute(
                    text("CREATE TABLE public.tier_catalog (tier_id text PRIMARY KEY, label text NOT NULL)")
                )
                await conn.execute(text("INSERT INTO public.tier_catalog (tier_id, label) VALUES ('team', 'Team')"))
            async with engine.begin() as conn:
                # No ``feature_flag_catalog`` table exists — both directions must
                # short-circuit on the information_schema guard rather than raise.
                swept = await conn.run_sync(lambda sync: module.sweep_stale_catalog_rows(sync))
                restored = await conn.run_sync(lambda sync: module.restore_retired_catalog_rows(sync))
            async with engine.connect() as conn:
                catalog_oid = (
                    await conn.execute(text("SELECT to_regclass('public.feature_flag_catalog')"))
                ).scalar_one()
                tiers = (await conn.execute(text("SELECT tier_id, label FROM public.tier_catalog"))).all()
        finally:
            await engine.dispose()
        # Guard held: no catalog table was created AND the pre-existing state is
        # exactly as seeded (an unguarded statement would have raised 42P01).
        assert catalog_oid is None
        assert sorted(tiers) == [("team", "Team")]
        assert swept == 0
        assert restored == 0
    finally:
        await _drop_db(db_url, db_name)


async def test_guarded_sql_is_noop_when_name_column_absent(db_url: str) -> None:
    db_name, iso_url = await _fresh_db(db_url, "m0317_nocol")
    try:
        engine = create_async_engine(iso_url, poolclass=NullPool)
        try:
            module = _migration_module()
            async with engine.begin() as conn:
                # A ``feature_flag_catalog`` table WITHOUT its ``name`` column,
                # carrying one sentinel row. The column guard must short-circuit
                # both directions; an unguarded DELETE/INSERT would raise 42703.
                await conn.execute(text('CREATE TABLE public."feature_flag_catalog" (legacy_id integer)'))
                await conn.execute(text('INSERT INTO public."feature_flag_catalog" (legacy_id) VALUES (42)'))
            async with engine.begin() as conn:
                swept = await conn.run_sync(lambda sync: module.sweep_stale_catalog_rows(sync))
                restored = await conn.run_sync(lambda sync: module.restore_retired_catalog_rows(sync))
            async with engine.connect() as conn:
                sentinels = (
                    (await conn.execute(text('SELECT legacy_id FROM public."feature_flag_catalog"'))).scalars().all()
                )
        finally:
            await engine.dispose()
        assert list(sentinels) == [42]
        assert swept == 0
        assert restored == 0
    finally:
        await _drop_db(db_url, db_name)


async def test_restore_is_noop_when_tier_catalog_table_absent(db_url: str) -> None:
    db_name, iso_url = await _fresh_db(db_url, "m0317_notier")
    try:
        engine = create_async_engine(iso_url, poolclass=NullPool)
        try:
            module = _migration_module()
            async with engine.begin() as conn:
                # A present catalog table with its ``name`` column but NO
                # ``tier_catalog`` table: the restore's tier-table guard must
                # short-circuit (an unguarded INSERT would raise 42P01).
                await conn.execute(
                    text(
                        'CREATE TABLE public."feature_flag_catalog" ('
                        "name text PRIMARY KEY, description text, tier_id text, depends_on json, is_active boolean)"
                    )
                )
            async with engine.begin() as conn:
                restored = await conn.run_sync(lambda sync: module.restore_retired_catalog_rows(sync))
            async with engine.connect() as conn:
                tier_oid = (await conn.execute(text("SELECT to_regclass('public.tier_catalog')"))).scalar_one()
                remaining = (
                    await conn.execute(text('SELECT count(*) FROM public."feature_flag_catalog"'))
                ).scalar_one()
        finally:
            await engine.dispose()
        assert tier_oid is None
        assert restored == 0
        assert remaining == 0
    finally:
        await _drop_db(db_url, db_name)


async def test_downgrade_restores_the_retired_rows(prev_rev_db_url: str) -> None:
    module = _migration_module()
    retired = tuple(sorted(module.RETIRED_FLAG_NAMES))
    await _seed_catalog(prev_rev_db_url, flags=retired)
    _run_alembic(prev_rev_db_url, "upgrade", MIGRATION_REV)
    assert RETIRED_FLAG not in await _table_flag_names(prev_rev_db_url)
    assert not await _table_flag_names(prev_rev_db_url)

    _run_alembic(prev_rev_db_url, "downgrade", PREV_REV)

    restored = await _table_flag_names(prev_rev_db_url)
    assert restored == set(retired)
