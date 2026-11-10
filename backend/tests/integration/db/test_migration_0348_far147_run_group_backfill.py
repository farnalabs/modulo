"""Integration test for migration 0348_far147_legacy_run_group_backfill (FAR-147).

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0302`` / ``test_migration_0215`` harness):

* seed a legacy-era ``runs`` table at ``0347_system_maps_version_aware`` with
  the run-group columns present and NULL, covering every classification case;
* run the REAL alembic upgrade of ``0348_far147_legacy_run_group_backfill``
  (which executes the migration's recursive CTE through ``op.get_bind()``)
  and assert:
  - a rerun child on a fresh thread joins the top root as ``retry``,
  - a child sharing its immediate parent's thread is ``resume``,
  - a grandchild is grouped to the TOP root while classified by its IMMEDIATE
    parent's thread,
  - ``correction`` / ``agent_signal`` children keep NULL group and kind,
  - a child of an already-grouped ancestor joins that EXISTING group root
    (not the topmost run of the parent chain),
  - roots stay ungrouped;
* execute the production recursive CTE a second time and assert idempotence
  (the full grouping snapshot is unchanged).

The resume candidate is seeded with ``run_group_kind='resume'`` and a NULL
``run_group_id`` because the live ``uq_runs_thread_non_resume`` partial unique
index forbids a NULL-kind row sharing its parent's thread; the row is still a
backfill candidate (NULL group) and the backfill must preserve the resume
classification while filling the group id.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/
MIGRATION_REV = "0348_far147_legacy_run_group_backfill"
PREV_REV = "0347_system_maps_version_aware"
_MIGRATION_PATH = BACKEND_ROOT / "src/farna/db/migrations/versions/0348_far147_legacy_run_group_backfill.py"


# ---------------------------------------------------------------------------
# Harness (mirrors test_migration_0302_runs_run_grouping.py)
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "farna" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API."""
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
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A private Postgres database migrated to ``PREV_REV`` (0347)."""
    db_name, iso_url = await _fresh_db(db_url, "m0348_pre")
    try:
        _migrate(iso_url, PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("migration_0348_it", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Seed account/org/pipeline/snapshot plus the legacy run tree at 0347."""
    ids = {
        "account": uuid.uuid4(),
        "org": uuid.uuid4(),
        "pipeline": uuid.uuid4(),
        "snapshot": uuid.uuid4(),
        "root": uuid.uuid4(),
        "retry_child": uuid.uuid4(),
        "resume_child": uuid.uuid4(),
        "correction_child": uuid.uuid4(),
        "signal_child": uuid.uuid4(),
        "grandchild": uuid.uuid4(),
        "top": uuid.uuid4(),
        "existing_group_root": uuid.uuid4(),
        "grouped_parent": uuid.uuid4(),
        "existing_group_child": uuid.uuid4(),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, 'm0348@example.com', 'm0348', 'hash', 'local', true)"
            ),
            {"id": str(ids["account"])},
        )
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'm0348', 'm0348', '{}'::json)"
            ),
            {"id": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :aid, 'm0348 pipeline', 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(ids["pipeline"]), "oid": str(ids["org"]), "aid": str(ids["account"])},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(ids["snapshot"]), "pid": str(ids["pipeline"]), "oid": str(ids["org"])},
        )

        # (key, thread, trigger_type, parent_key, seeded group key, seeded kind)
        rows: list[tuple[str, str, str, str | None, str | None, str | None]] = [
            ("root", "thread-root", "manual", None, None, None),
            ("retry_child", "thread-retry", "rerun", "root", None, None),
            # Resume candidate: seeded kind='resume' (the live partial unique
            # index forbids NULL-kind shared threads); group id is NULL, so the
            # backfill still has to fill it.
            ("resume_child", "thread-root", "rerun", "root", None, "resume"),
            ("correction_child", "thread-correction", "correction", "root", None, None),
            ("signal_child", "thread-signal", "agent_signal", "root", None, None),
            ("grandchild", "thread-grandchild", "rerun", "retry_child", None, None),
            ("top", "thread-top", "manual", None, None, None),
            ("existing_group_root", "thread-group-root", "manual", None, None, None),
            # Already grouped attempt whose child must join ITS group, not the
            # topmost run of the parent chain.
            ("grouped_parent", "thread-grouped-parent", "rerun", "top", "existing_group_root", "retry"),
            ("existing_group_child", "thread-existing-child", "rerun", "grouped_parent", None, None),
        ]
        for index, (key, thread, trigger_type, parent_key, group_key, kind) in enumerate(rows, start=1):
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                    "status, run_number, input_hash, langgraph_thread_id, parent_run_id, "
                    "run_group_id, run_group_kind) "
                    "VALUES (:id, :oid, :pid, :sid, :trigger_type, 'complete', :n, 'ih', :thread, "
                    ":parent, :group, :kind)"
                ),
                {
                    "id": str(ids[key]),
                    "oid": str(ids["org"]),
                    "pid": str(ids["pipeline"]),
                    "sid": str(ids["snapshot"]),
                    "n": index,
                    "trigger_type": trigger_type,
                    "thread": thread,
                    "parent": str(ids[parent_key]) if parent_key else None,
                    "group": str(ids[group_key]) if group_key else None,
                    "kind": kind,
                },
            )
    return ids


async def _grouping(engine: AsyncEngine) -> dict[str, tuple[str | None, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id, run_group_id, run_group_kind FROM runs"))
        return {str(row[0]): (str(row[1]) if row[1] is not None else None, row[2]) for row in rows.all()}


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())


# ---------------------------------------------------------------------------
# The production backfill
# ---------------------------------------------------------------------------


async def test_upgrade_backfills_only_genuine_retry_resume_children(pre_upgrade_db_url: str) -> None:
    engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
    try:
        ids = await _seed(engine)
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        assert await _alembic_version(engine) == MIGRATION_REV

        grouped = await _grouping(engine)
        root_id = str(ids["root"])
        existing_group_root_id = str(ids["existing_group_root"])
        top_id = str(ids["top"])

        # Roots stay ungrouped.
        assert grouped[root_id] == (None, None)
        assert grouped[top_id] == (None, None)
        assert grouped[existing_group_root_id] == (None, None)

        # Rerun child on a fresh thread: retry of the root.
        assert grouped[str(ids["retry_child"])] == (root_id, "retry")
        # Child sharing its immediate parent's thread: resume of the root.
        assert grouped[str(ids["resume_child"])] == (root_id, "resume")
        # Grandchild: grouped to the TOP root, classified by its immediate
        # parent's thread (a fresh thread => retry).
        assert grouped[str(ids["grandchild"])] == (root_id, "retry")

        # Correction / agent_signal children keep NULL grouping.
        assert grouped[str(ids["correction_child"])] == (None, None)
        assert grouped[str(ids["signal_child"])] == (None, None)

        # A child of an already-grouped attempt joins that existing group root
        # — not the topmost run of the parent chain.
        assert grouped[str(ids["grouped_parent"])] == (existing_group_root_id, "retry")
        assert grouped[str(ids["existing_group_child"])] == (existing_group_root_id, "retry")
    finally:
        await engine.dispose()


async def test_backfill_cte_is_idempotent(pre_upgrade_db_url: str) -> None:
    engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
    try:
        await _seed(engine)
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        first_pass = await _grouping(engine)

        migration = _load_migration()
        statement = migration._postgres_backfill_statement()
        params = migration._postgres_backfill_params()
        async with engine.begin() as conn:
            await conn.execute(statement, params)
        second_pass = await _grouping(engine)
        async with engine.begin() as conn:
            await conn.execute(statement, params)
        third_pass = await _grouping(engine)

        assert first_pass == second_pass
        assert first_pass == third_pass
    finally:
        await engine.dispose()
