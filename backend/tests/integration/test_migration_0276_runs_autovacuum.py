"""FAR-1419: the unrecorded ``autovacuum_enabled=false`` reloption on ``runs``, end to end.

Production's ``runs`` table carries ``autovacuum_enabled=false`` as deployed
state that NOTHING in this repository records - a repo-wide search for
``autovacuum`` before 0276 returned zero matches across migrations, models,
compose files and deploy configs. The consequence (prod, 2026-10-02):
``n_live_tup=10,411`` vs ``n_dead_tup=361,302`` (~97% dead), a 7.5 GB table
with ``last_autovacuum=never``, ~11.66% disk reads and ~2.8 s health-check
latency (staging: 474 ms) - prod readiness intermittently 504s. Autovacuum is
globally ON and vacuums every other table; ``runs`` is the sole exception.

Because a fresh CI/integration database NEVER carries the out-of-band
reloption, replaying the chain cannot reproduce the defect either - so this
module builds it deliberately:

* a private database migrated to ``0275`` (the chain head before this fix),
  then ``ALTER TABLE ... SET (autovacuum_enabled = false)`` - the deployed
  state, verbatim;
* the NEGATIVE CONTROL: the enabling assertion from
  :func:`_assert_runs_autovacuum_enabled` is run against that state and must
  FAIL (``pytest.raises(AssertionError)``) - i.e. without 0276 the test's
  post-migration assertion does not pass;
* the REAL alembic upgrade of ``0276_runs_autovacuum_enabled`` runs, and the
  same assertion then passes (``autovacuum_enabled = true``);
* re-running the revision is a no-op (idempotent ``SET``), and the symmetric
  downgrade ``RESET``s the reloption so a downgraded schema matches a fresh
  pre-0276 database and can never re-disable the table.

A second test drives the FRESH-database path: the whole chain straight to
0276 on a brand-new database (table created, then the reloption written), so
"safe on a fresh database" is observed rather than assumed.

No assertion here depends on autovacuum's wall-clock scheduling - the test
proves eligibility (the reloption), never that a background worker happened to
run. That keeps it deterministic.

The private-database fixture mirrors
``test_migration_0275_run_cancel_reason_vocabulary.py``.
"""

from __future__ import annotations

import uuid
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
MIGRATION_REV = "0276_runs_autovacuum_enabled"
PREV_REV = "0275_run_cancel_reason_vocabulary"

# The deployed defect, reproduced verbatim: the out-of-band reloption nothing
# in the repo ever recorded.
_OUT_OF_BAND_DISABLE = 'ALTER TABLE public."runs" SET (autovacuum_enabled = false);'


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


@pytest_asyncio.fixture
async def pre_fix_db_url(db_url: str):
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    ``env.py`` resolves the target DB from ``DATABASE_URL`` /
    ``DATABASE_ADMIN_URL``, so both are pinned for every alembic call."""
    db_name, iso_url = await _fresh_db(db_url, "m0276_pre")
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()

    _migrate(iso_url, PREV_REV)

    yield iso_url

    await _drop_db(db_url, db_name)


@pytest_asyncio.fixture
async def fresh_chain_db_url(db_url: str):
    """A fresh, private Postgres database migrated ALL the way to ``MIGRATION_REV``.

    This is the "safe on a fresh database" path: the whole chain (table
    creation included) replays on a brand-new database, so 0276 must apply on
    top of a just-created ``runs`` without any gate."""
    db_name, iso_url = await _fresh_db(db_url, "m0276_fresh")
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()

    _migrate(iso_url, MIGRATION_REV)

    yield iso_url

    await _drop_db(db_url, db_name)


async def _autovacuum_enabled(engine: AsyncEngine) -> str | None:
    """The ``runs`` table's ``autovacuum_enabled`` reloption value.

    ``None`` means the reloption is not recorded at all - the table then
    inherits the server default, which IS enabled."""
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT reloptions FROM pg_class WHERE oid = 'public.runs'::regclass"))
        options = row.scalar_one_or_none()
    if options is None:
        return None
    for entry in options:
        key, sep, value = entry.partition("=")
        if sep and key == "autovacuum_enabled":
            return value
    return None


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return str(result.scalar_one())


async def _assert_runs_autovacuum_enabled(engine: AsyncEngine) -> None:
    """Assert ``runs`` is eligible for autovacuum (the reloption is not off).

    This is the assertion that FAILS against the pre-0276 deployed state -
    the negative control in ``TestOutOfBandDisable``."""
    state = await _autovacuum_enabled(engine)
    assert state != "false", (
        "runs has autovacuum_enabled=false - the table is excluded from autovacuum "
        "(FAR-1419: n_dead_tup 361302 vs n_live_tup 10411, last_autovacuum=never)"
    )


class TestOutOfBandDisable:
    async def test_deployed_state_is_repaired_by_0276(self, pre_fix_db_url: str) -> None:
        """The FAR-1419 failure and its fix, in one sequence against real
        Postgres: the out-of-band reloption is reproduced, the enabling
        assertion FAILS against it (negative control), the real alembic
        upgrade of 0276 turns it back on, and the same assertion then passes.
        Re-running the revision afterwards is a no-op.

        Without 0276 this test fails at the post-migration assertion: with the
        revision gone the chain head is still 0275, the upgrade is a no-op
        (alembic has nothing to apply), the deployed reloption survives, and
        the final assertion still sees ``false`` - the pre-migration half is
        the live production defect reproduced verbatim either way."""
        engine = create_async_engine(pre_fix_db_url, poolclass=NullPool)
        try:
            # 1. Reproduce the deployed world: the reloption nobody recorded.
            async with engine.begin() as conn:
                await conn.execute(text(_OUT_OF_BAND_DISABLE))
            state = await _autovacuum_enabled(engine)
            assert state == "false", f"the deployed out-of-band state must be reproduced, got {state!r}"

            # 2. NEGATIVE CONTROL - the enabling assertion FAILS pre-migration.
            with pytest.raises(AssertionError, match="autovacuum_enabled=false"):
                await _assert_runs_autovacuum_enabled(engine)

            # 3. The REAL migration repairs it.
            _migrate(pre_fix_db_url, MIGRATION_REV)

            await _assert_runs_autovacuum_enabled(engine)
            assert await _autovacuum_enabled(engine) == "true", "0276 must WRITE the reloption, not just clear it"
            assert await _alembic_version(engine) == MIGRATION_REV

            # 4. Idempotent: re-running the revision rewrites the same value.
            _migrate(pre_fix_db_url, MIGRATION_REV)
            await _assert_runs_autovacuum_enabled(engine)
            assert await _autovacuum_enabled(engine) == "true"
        finally:
            await engine.dispose()

    async def test_downgrade_resets_the_reloption_without_disabling(
        self,
        pre_fix_db_url: str,
    ) -> None:
        """The symmetric downgrade: ``RESET`` drops the reloption entirely, so
        a downgraded schema matches a fresh pre-0276 database (table inherits
        the server default - enabled) and can never re-disable the table."""
        engine = create_async_engine(pre_fix_db_url, poolclass=NullPool)
        try:
            _migrate(pre_fix_db_url, MIGRATION_REV)
            assert await _autovacuum_enabled(engine) == "true"

            with pytest.MonkeyPatch().context() as mp:
                mp.setenv("DATABASE_URL", pre_fix_db_url)
                mp.setenv("DATABASE_ADMIN_URL", pre_fix_db_url)
                _alembic_cmd(pre_fix_db_url, "downgrade", PREV_REV)

            assert await _alembic_version(engine) == PREV_REV
            state = await _autovacuum_enabled(engine)
            assert state is None, f"RESET must drop the reloption entirely, got {state!r}"
            await _assert_runs_autovacuum_enabled(engine)

            # And the upgrade re-applies cleanly after a downgrade.
            _migrate(pre_fix_db_url, MIGRATION_REV)
            assert await _autovacuum_enabled(engine) == "true"
        finally:
            await engine.dispose()


class TestFreshDatabase:
    async def test_fresh_chain_enables_autovacuum_on_runs(self, fresh_chain_db_url: str) -> None:
        """Safe on a fresh database: the full chain to 0276 replays on a
        brand-new database (``runs`` created, then the reloption written) and
        the table ends up explicitly enabled."""
        engine = create_async_engine(fresh_chain_db_url, poolclass=NullPool)
        try:
            assert await _alembic_version(engine) == MIGRATION_REV
            await _assert_runs_autovacuum_enabled(engine)
            assert await _autovacuum_enabled(engine) == "true"
        finally:
            await engine.dispose()
