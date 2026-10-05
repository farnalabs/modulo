"""FAR-1443: drop the three genuinely unused ``runs`` indexes, end to end.

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0279_table_autovacuum_tuning.py`` pattern):

* NEGATIVE CONTROL — at ``0280_runs_node_deadline_watchdog_fired_count`` all
  three indexes EXIST, so the post-migration "absent" assertion fails
  against the pre-migration state and the test discriminates rather than
  passing vacuously;
* the REAL alembic upgrade of ``0281_runs_drop_unused_indexes`` leaves
  ``ix_runs_account_id`` / ``ix_runs_dispatcher`` /
  ``ix_runs_org_status_completed`` absent from ``pg_indexes``, while a
  control set (every constraint plus the narrow
  ``ix_runs_organisation_status`` sibling and the hot sweep/list indexes)
  survives untouched;
* the DOWNGRADE restores every dropped index with its ORIGINAL definition —
  asserted against the exact ``pg_indexes.indexdef`` string, so "restored"
  means the same key columns, not merely a same-named index;
* re-upgrading after the downgrade drops them again (the round trip is
  repeatable);
* model parity — the ``Run`` ORM model no longer declares any of the three
  (``ix_runs_account_id``/``ix_runs_dispatcher`` were ``index=True`` flags,
  ``ix_runs_org_status_completed`` was migration-only and never in the
  model), so the migrated schema and the model agree.

No assertion depends on planner behaviour or ``pg_stat`` counters — the
audit's usage evidence lives in the PR description; these tests prove the
SCHEMA change and its reversibility.
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

from modulo.db.models.run import Run

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0281_runs_drop_unused_indexes"
PREV_REV = "0280_runs_node_deadline_watchdog_fired_count"

_DROPPED: tuple[str, ...] = (
    "ix_runs_account_id",
    "ix_runs_dispatcher",
    "ix_runs_org_status_completed",
)

#: The exact ``pg_indexes.indexdef`` the downgrade must restore — pins
#: "with the same definition", not merely "an index with that name".
_EXPECTED_INDEXDEF: dict[str, str] = {
    "ix_runs_account_id": "CREATE INDEX ix_runs_account_id ON public.runs USING btree (account_id)",
    "ix_runs_dispatcher": "CREATE INDEX ix_runs_dispatcher ON public.runs USING btree (dispatcher)",
    "ix_runs_org_status_completed": (
        "CREATE INDEX ix_runs_org_status_completed ON public.runs USING btree (organisation_id, status, completed_at)"
    ),
}

#: Every index the migration must NOT touch: the constraints (never droppable
#: per the task contract), the narrow (org, status) sibling whose 110k scans
#: are the usage evidence for dropping its composite superset, and the hot
#: list/sweep indexes from the same audit.
_SURVIVORS: tuple[str, ...] = (
    "runs_pkey",
    "runs_langgraph_thread_id_key",
    "uq_runs_org_run_number",
    "uq_runs_idempotency",
    "uq_runs_pipeline_rate_limit_key",
    "ix_runs_organisation_status",
    "ix_runs_organisation_id",
    "ix_runs_org_created_pipeline",
    "ix_runs_probe",
    "ix_runs_workspace_drift_sweep",
    "ix_runs_unclassified_terminal",
)


# ---------------------------------------------------------------------------
# Harness (mirrors test_migration_0279_table_autovacuum_tuning.py)
# ---------------------------------------------------------------------------


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
    """A private Postgres database migrated to ``PREV_REV`` — the deployed
    shape, where all three indexes still exist."""
    db_name, iso_url = await _new_private_db(db_url, "m0281_pre")
    try:
        _migrate(iso_url, PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _runs_indexdefs(engine: AsyncEngine) -> dict[str, str]:
    """``{indexname: indexdef}`` for every index on ``public.runs``."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'runs'")
        )
        return {str(name): str(indexdef) for name, indexdef in rows.all()}


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return str(result.scalar_one())


def _assert_dropped_absent(indexdefs: dict[str, str]) -> None:
    present = [name for name in _DROPPED if name in indexdefs]
    assert not present, f"dropped index(es) still present after {MIGRATION_REV}: {present}"


def _assert_survivors_present(indexdefs: dict[str, str]) -> None:
    missing = [name for name in _SURVIVORS if name not in indexdefs]
    assert not missing, f"migration dropped indexes it must not touch: {missing}"


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


class TestUpgrade:
    async def test_negative_control_then_drop_with_survivors_intact(self, pre_upgrade_db_url: str) -> None:
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            before = await _runs_indexdefs(engine)
            for name in _DROPPED:
                assert name in before, f"{name} must EXIST at {PREV_REV} (negative control)"
            _assert_survivors_present(before)

            # NEGATIVE CONTROL: the post-migration assertion must FAIL
            # against the pre-migration state, so it discriminates.
            with pytest.raises(AssertionError, match="still present"):
                _assert_dropped_absent(before)

            # The REAL migration.
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV

            after = await _runs_indexdefs(engine)
            _assert_dropped_absent(after)
            _assert_survivors_present(after)

            # Re-running the recorded revision is a no-op (state holds).
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            _assert_dropped_absent(await _runs_indexdefs(engine))
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Downgrade — the one-way-door guard
# ---------------------------------------------------------------------------


class TestDowngrade:
    async def test_downgrade_restores_every_index_with_its_original_definition(
        self,
        pre_upgrade_db_url: str,
    ) -> None:
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            _assert_dropped_absent(await _runs_indexdefs(engine))

            _downgrade(pre_upgrade_db_url, PREV_REV)
            assert await _alembic_version(engine) == PREV_REV

            restored = await _runs_indexdefs(engine)
            for name, expected_def in _EXPECTED_INDEXDEF.items():
                assert name in restored, f"downgrade must restore {name}"
                assert restored[name] == expected_def, (
                    f"{name} restored with a DIFFERENT definition:\n  want: {expected_def}\n  got:  {restored[name]}"
                )
            _assert_survivors_present(restored)

            # The round trip is repeatable: upgrade drops them again.
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV
            _assert_dropped_absent(await _runs_indexdefs(engine))
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Model parity (no database required)
# ---------------------------------------------------------------------------


class TestModelParity:
    def test_model_declares_none_of_the_dropped_indexes(self) -> None:
        """Migration and model must be dropped TOGETHER (the database-domain
        parity rule): a model still declaring a dropped index is drift the
        reviewer blocks on."""
        declared = {idx.name for idx in Run.__table__.indexes if idx.name is not None}
        still_declared = sorted(name for name in _DROPPED if name in declared)
        assert not still_declared, f"Run model still declares dropped index(es): {still_declared}"
        # The two column flags specifically — index=True would re-create them
        # on any create_all-built schema.
        assert not Run.__table__.c.account_id.index, "runs.account_id must not carry index=True"
        assert not Run.__table__.c.dispatcher.index, "runs.dispatcher must not carry index=True"

    def test_model_keeps_the_indexes_the_audit_kept(self) -> None:
        """Positive control on the model side: the audit kept
        ix_runs_org_error_code / ix_runs_variant_group_batch /
        ix_runs_org_created_pipeline / ix_runs_workspace_drift_sweep — they
        must still be declared, so a too-broad edit fails here."""
        declared = {idx.name for idx in Run.__table__.indexes if idx.name is not None}
        kept = (
            "ix_runs_org_error_code",
            "ix_runs_variant_group_batch",
            "ix_runs_org_created_pipeline",
            "ix_runs_workspace_drift_sweep",
        )
        missing = [name for name in kept if name not in declared]
        assert not missing, f"Run model lost kept index declaration(s): {missing}"
