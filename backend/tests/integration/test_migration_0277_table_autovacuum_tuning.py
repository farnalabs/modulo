"""FAR-1442: per-table autovacuum tuning on ``runs``, ``checkpoints`` and ``checkpoint_writes``, end to end.

Postgres' autovacuum defaults gate a vacuum on 50 dead rows PLUS 20% of a
table's live rows (``autovacuum_vacuum_scale_factor = 0.20``), and cap the
scan rate at ``vacuum_cost_limit`` / ``autovacuum_vacuum_cost_delay`` =
200 units / 20 ms = ~10,000 cost units/s (~4-8 MB/s). Measured on
production 2026-10-03, those defaults are wrong for Modulo's table shape:

* ``checkpoints`` - 9,436 MB, 180,374 lifetime deletes;
* ``checkpoint_writes`` - 5,893 MB, **2,175,755** lifetime deletes;
* ``runs`` - 80 MB post-VACUUM, heavy insert/update churn + retention
  deletes (FAR-1419 measured 361,302 dead vs 10,411 live while autovacuum
  was off).

So this module asserts, against REAL Postgres (testcontainers):

* the NEGATIVE CONTROL - the tuned reloptions are absent BEFORE 0277, and
  the post-migration assertion FAILS against that pre-migration state, so
  the test discriminates rather than passing vacuously;
* the REAL alembic upgrade of 0277 lands every expected reloption value on
  all three tables (``checkpoints``/``checkpoint_writes`` are created first
  via ``ModuloPostgresSaver._MIGRATION_SQL``, exactly as a deployed
  database has them), and re-running the revision is a no-op;
* ``ALTER TABLE ... SET`` MERGES with existing reloptions - both as raw
  Postgres semantics on a scratch relation and as the migration-level
  consequence (an unrelated ``fillfactor`` reloption placed on
  ``checkpoints`` before the upgrade survives it, while 0276's
  ``autovacuum_enabled=true`` on ``runs`` sits alongside the new tuning);
* the downgrade RESETs exactly what the upgrade added while leaving
  0276's ``autovacuum_enabled=true`` in place;
* the FRESH-database path - the whole chain straight to 0277 on a
  brand-new database, where the checkpoint tables do NOT exist yet
  (alembic runs before ``ModuloPostgresSaver.setup()``), so the
  existence-gated ALTER must skip them (loudly, via NOTICE) instead of
  failing the chain.

No assertion depends on autovacuum's wall-clock scheduling - the tests
prove the CONFIGURATION, never that a background worker happened to run.
That keeps them deterministic.

The private-database fixture mirrors ``test_migration_0276_runs_autovacuum.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0277_table_autovacuum_tuning"
PREV_REV = "0276_runs_autovacuum_enabled"

#: The exact reloption set 0277 must leave on each table.
#:
#: Values, justified from the measured production churn (see the migration
#: docstring): 0.02/0.01 on the multi-GB checkpoint tables cut the
#: dead-tuple budget 10x below the default 0.20/0.10 and refresh planner
#: stats 10x sooner; cost_limit 10000 @ cost_delay 2 ms raises the scan
#: rate ~500x over the default 200 @ 20 ms so a full pass over 9,436 MB
#: finishes in seconds; ``runs`` (80 MB, high churn) gets a tighter
#: 0.05/0.02 gate plus a restated ``autovacuum_enabled = true``.
_EXPECTED_RELOPTIONS: dict[str, dict[str, str]] = {
    "runs": {
        "autovacuum_enabled": "true",
        "autovacuum_vacuum_scale_factor": "0.05",
        "autovacuum_analyze_scale_factor": "0.02",
    },
    "checkpoints": {
        "autovacuum_vacuum_scale_factor": "0.02",
        "autovacuum_analyze_scale_factor": "0.01",
        "autovacuum_vacuum_cost_limit": "10000",
        "autovacuum_vacuum_cost_delay": "2",
    },
    "checkpoint_writes": {
        "autovacuum_vacuum_scale_factor": "0.02",
        "autovacuum_analyze_scale_factor": "0.01",
        "autovacuum_vacuum_cost_limit": "10000",
        "autovacuum_vacuum_cost_delay": "2",
    },
}

#: Every option key this revision writes, used to prove the PRE-migration
#: state carries none of them (the negative control).
_TUNED_KEYS: tuple[str, ...] = (
    "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_scale_factor",
    "autovacuum_vacuum_cost_limit",
    "autovacuum_vacuum_cost_delay",
)


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    Deliberately does NOT inject ``cmd_opts`` (the FAR-967 F2 shape): env.py
    infers direction from the active EnvironmentContext instead.
    """
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    """Create an empty private database; return ``(name, async url)``."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


def _migrate(iso_url: str, target: str) -> None:
    """Run an alembic upgrade with env.py's URL resolution pinned to ``iso_url``."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", target)


def _downgrade(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "downgrade", target)


async def _new_private_db(db_url: str, prefix: str) -> tuple[str, str]:
    """Empty private database with an ``alembic_version`` stub; ``(name, async url)``."""
    db_name, iso_url = await _fresh_db(db_url, prefix)
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()
    return db_name, iso_url


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A private Postgres database at ``PREV_REV`` WITH the runtime-owned
    checkpoint tables - the shape of every already-deployed database: the
    application booted (``ModuloPostgresSaver.setup()`` created the LangGraph
    checkpoint tables) long before this revision arrives."""
    db_name, iso_url = await _new_private_db(db_url, "m0277_pre")
    try:
        _migrate(iso_url, PREV_REV)

        # The checkpoint tables are created at app startup, not by alembic.
        from modulo.core.pipeline_engine.modulo_saver import _MIGRATION_SQL as CHECKPOINT_MIGRATION_SQL

        engine = create_async_engine(iso_url, poolclass=NullPool)
        async with engine.begin() as conn:
            for ddl in CHECKPOINT_MIGRATION_SQL:
                await conn.execute(text(ddl))
        await engine.dispose()

        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


@pytest_asyncio.fixture
async def fresh_chain_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A brand-new private database migrated ALL the way to ``MIGRATION_REV``.

    On this path the checkpoint tables do not exist (alembic runs before
    ``ModuloPostgresSaver.setup()``), so 0277 must skip them rather than
    fail the chain."""
    db_name, iso_url = await _new_private_db(db_url, "m0277_fresh")
    try:
        _migrate(iso_url, MIGRATION_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _reloptions(engine: AsyncEngine, table: str) -> dict[str, str]:
    """``pg_class.reloptions`` for ``public.<table>`` as ``{key: value}``.

    Returns an empty mapping when the table has no reloptions at all (the
    table then inherits the server defaults)."""
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT reloptions FROM pg_class WHERE oid = to_regclass(:name)"),
            {"name": f"public.{table}"},
        )
        options = row.scalar_one_or_none()
    if options is None:
        return {}
    parsed: dict[str, str] = {}
    for entry in options:
        key, sep, value = entry.partition("=")
        if sep:
            parsed[key] = value
    return parsed


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"})
        return bool(row.scalar_one())


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return str(result.scalar_one())


async def _assert_table_tuned(engine: AsyncEngine, table: str) -> None:
    """Every expected reloption on ``table`` must be present with its exact value.

    This is the assertion that FAILS against the pre-0277 state - the
    negative control below."""
    expected = _EXPECTED_RELOPTIONS[table]
    actual = await _reloptions(engine, table)
    for key, want in expected.items():
        assert actual.get(key) == want, (
            f"{table}.{key}: expected {want!r}, got {actual.get(key)!r} (full reloptions: {actual!r})"
        )


async def _assert_no_tuning(engine: AsyncEngine, table: str) -> None:
    """Pre-migration control: NONE of the tuned keys may be present yet."""
    actual = await _reloptions(engine, table)
    for key in _TUNED_KEYS:
        assert key not in actual, f"{table} already carries {key}={actual.get(key)!r} before {MIGRATION_REV}"


class TestUpgrade:
    async def test_negative_control_then_upgrade_tunes_all_three_tables(self, pre_upgrade_db_url: str) -> None:
        """The failure and its fix, in one sequence against real Postgres.

        NEGATIVE CONTROL first: the post-migration assertion must FAIL
        against the pre-0277 state (so the test discriminates), then the
        REAL alembic upgrade of 0277 must make it pass on all three tables,
        and re-running the revision must be a no-op."""
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            # 0. The deployed shape: 0276 has already enabled runs, and the
            #    checkpoint tables exist with no reloptions at all.
            assert await _reloptions(engine, "runs") == {"autovacuum_enabled": "true"}
            for table in ("checkpoints", "checkpoint_writes"):
                assert await _table_exists(engine, table)
                await _assert_no_tuning(engine, table)

            # 1. NEGATIVE CONTROL - the post-migration assertion FAILS pre-migration.
            with pytest.raises(AssertionError, match="autovacuum_vacuum_scale_factor"):
                await _assert_table_tuned(engine, "runs")
            with pytest.raises(AssertionError, match="autovacuum_vacuum_scale_factor"):
                await _assert_table_tuned(engine, "checkpoints")
            with pytest.raises(AssertionError, match="autovacuum_vacuum_scale_factor"):
                await _assert_table_tuned(engine, "checkpoint_writes")
            await _assert_no_tuning(engine, "runs")

            # 2. The REAL migration lands the tuning on all three tables.
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV
            for table in _EXPECTED_RELOPTIONS:
                await _assert_table_tuned(engine, table)

            # 0276's enablement is still there - the tuning came on top of it.
            assert (await _reloptions(engine, "runs"))["autovacuum_enabled"] == "true"

            # 3. Idempotent: re-running the revision rewrites the same values.
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            for table in _EXPECTED_RELOPTIONS:
                await _assert_table_tuned(engine, table)
        finally:
            await engine.dispose()


class TestAlterTableSetSemantics:
    async def test_set_merges_with_existing_reloptions(self, pre_upgrade_db_url: str) -> None:
        """``ALTER TABLE ... SET`` MERGES - it never replaces the whole reloption set.

        This is the subtle premise the migration's correctness rests on, so
        it is pinned twice: once as the migration-level consequence (an
        unrelated ``fillfactor`` reloption placed on ``checkpoints`` before
        the upgrade survives it, while 0276's ``autovacuum_enabled=true`` on
        ``runs`` sits alongside the new tuning) and once as raw Postgres
        semantics on a scratch relation, independent of alembic."""
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            # 1. Migration level: an out-of-band reloption the revision never
            #    mentions must survive the upgrade untouched.
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE public."checkpoints" SET (fillfactor = 90)'))
            assert (await _reloptions(engine, "checkpoints"))["fillfactor"] == "90"

            _migrate(pre_upgrade_db_url, MIGRATION_REV)

            after = await _reloptions(engine, "checkpoints")
            assert after["fillfactor"] == "90", f"SET must MERGE (leave unnamed reloptions alone), got {after!r}"
            await _assert_table_tuned(engine, "checkpoints")

            # 0276's runs reloption is likewise preserved alongside the new tuning.
            runs_after = await _reloptions(engine, "runs")
            assert runs_after["autovacuum_enabled"] == "true"
            assert runs_after["autovacuum_vacuum_scale_factor"] == "0.05"

            # 2. Raw Postgres semantics, no alembic involved: a second SET on
            #    the same relation must not drop what the first one wrote.
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE relopt_merge_probe (a int)"))
                await conn.execute(text("ALTER TABLE relopt_merge_probe SET (autovacuum_enabled = false)"))
                await conn.execute(text("ALTER TABLE relopt_merge_probe SET (autovacuum_vacuum_scale_factor = 0.05)"))
            probe = await _reloptions(engine, "relopt_merge_probe")
            assert probe["autovacuum_enabled"] == "false", f"SET replaced rather than merged: {probe!r}"
            assert probe["autovacuum_vacuum_scale_factor"] == "0.05", f"second SET lost: {probe!r}"
        finally:
            await engine.dispose()


class TestDowngrade:
    async def test_downgrade_resets_tuning_but_keeps_autovacuum_enabled(
        self,
        pre_upgrade_db_url: str,
    ) -> None:
        """The symmetric downgrade: RESET drops exactly what the upgrade
        added, and leaves 0276's ``autovacuum_enabled=true`` alone - a
        downgraded schema equals what 0276 left, and can never re-disable
        the table (the FAR-1419 defect)."""
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            await _assert_table_tuned(engine, "runs")

            _downgrade(pre_upgrade_db_url, PREV_REV)
            assert await _alembic_version(engine) == PREV_REV

            runs_after = await _reloptions(engine, "runs")
            assert runs_after == {"autovacuum_enabled": "true"}, (
                f"downgrade must keep only 0276's reloption, got {runs_after!r}"
            )
            for table in ("checkpoints", "checkpoint_writes"):
                left = await _reloptions(engine, table)
                assert not left, f"downgrade must drop every tuned option on {table}, got {left!r}"

            # And the upgrade re-applies cleanly after a downgrade.
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            for table in _EXPECTED_RELOPTIONS:
                await _assert_table_tuned(engine, table)
        finally:
            await engine.dispose()


class TestFreshDatabase:
    async def test_fresh_chain_tunes_runs_and_skips_missing_checkpoint_tables(
        self,
        fresh_chain_db_url: str,
    ) -> None:
        """Safe on a fresh database: alembic runs BEFORE
        ``ModuloPostgresSaver.setup()``, so the checkpoint tables do not
        exist yet and the existence-gated ALTER must SKIP them (with a
        NOTICE) rather than fail the chain - while ``runs``, which alembic
        owns, is tuned either way."""
        engine = create_async_engine(fresh_chain_db_url, poolclass=NullPool)
        try:
            assert await _alembic_version(engine) == MIGRATION_REV

            await _assert_table_tuned(engine, "runs")

            for table in ("checkpoints", "checkpoint_writes"):
                assert not await _table_exists(engine, table), (
                    f"{table} is runtime-created, it must be absent on a fresh alembic-only database"
                )
        finally:
            await engine.dispose()
