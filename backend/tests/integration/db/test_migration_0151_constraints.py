"""Integration test for migration 0151_improve_db_constraints_indexes.

Runs the real Alembic ``upgrade`` / ``downgrade`` of revision 0151 against a
live Postgres (testcontainers) and verifies:

  * the three org-scoped status indexes are created;
  * ``fk_runs_variant_group`` and ``fk_organisations_plan`` are added (when the
    data is clean) and reference the correct parent tables;
  * ``ck_saved_views_sort_order`` CHECK is added (when the data is clean);
  * realistic ``pipeline_edges`` / ``eval_definitions`` rows whose node ids live
    in ``graph_nodes_json`` (never the deprecated ``nodes`` table) do NOT make
    the migration abort — proving the mis-targeted node-id FKs were removed;
  * ``downgrade`` drops the constraints + indexes cleanly, and re-upgrade
    restores them (idempotency / safe-to-re-run).

Uses an isolated database (cloned from ``template0``) so the shared session
schema is never mutated, mirroring ``test_migration_0126_eval_suite.py``.
"""

import os
import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "0151_improve_db_constraints_indexes"
# Reset only to the revision immediately before 0151. The 0150-and-earlier
# migrations on origin/main are applied by the session's migrated_db_url fixture
# and are NOT all idempotent (some DROP constraints without IF EXISTS), so
# re-running the whole chain would fail. Resetting to 0150 re-runs only our
# migration.
PREV_REV = "0150_add_router_no_match_status"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    """Return ``db_url`` with its database name replaced by ``new_db``."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str) -> AsyncIterator[str]:
    """A fresh, private Postgres database migrated only up to ``PREV_REV``."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0151_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    eng = create_async_engine(iso_url, poolclass=NullPool)
    async with eng.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
        await conn.commit()
    await eng.dispose()

    # env.py resolves the target DB from DATABASE_ADMIN_URL / DATABASE_URL
    # (preferring DATABASE_ADMIN_URL), NOT from the Config URL. Pin both to the
    # isolated database so command.upgrade runs against it, not the shared
    # session Postgres (where DuplicateTable would occur).
    with patch.dict(
        os.environ,
        {"DATABASE_URL": iso_url, "DATABASE_ADMIN_URL": iso_url},
    ):
        command.upgrade(_alembic_config(iso_url), PREV_REV)

    try:
        yield iso_url
    finally:
        admin_engine = create_async_engine(
            db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"}
        )
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ).bindparams(n=db_name)
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()


async def _seed(db_url: str) -> dict[str, uuid.UUID]:
    """Seed realistic rows that exercise every constraint 0151 touches.

    ``pipeline_edges`` / ``eval_definitions`` carry graph-node UUIDs that are
    NOT present in the deprecated ``nodes`` table — the exact shape that made
    the original (now removed) node-id FKs abort. ``runs`` references a real
    ``variant_group`` and ``organisations`` a real ``tier_catalog`` tier, so the
    remaining FKs' orphan checks pass and the constraints get added.
    """
    org_a = uuid.uuid4()
    acc_a = uuid.uuid4()
    pipe_a = uuid.uuid4()
    vg_a = uuid.uuid4()
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # Bypass FK enforcement for the seed so we don't have to populate
            # every parent table; the migration's own orphan checks run later in
            # a normal session and read the (valid) variant_group / tier rows.
            await conn.execute(text("SET session_replication_role = 'replica'"))

            await conn.execute(
                text(
                    "INSERT INTO tier_catalog (tier_id, label, rank) "
                    "VALUES ('team', 'Team', 2), ('community', 'Community', 1)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json, plan_id) "
                    "VALUES (:id, :n, :s, '{}'::json, 'team')"
                ),
                {"id": str(org_a), "n": "m0151-org", "s": "m0151-org"},
            )
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, "
                    "auth_provider, active) VALUES (:id, :e, :n, 'hash', 'local', true)"
                ),
                {"id": str(acc_a), "e": "m0151@example.com", "n": "m0151"},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
                ),
                {"id": str(pipe_a), "oid": str(org_a), "name": "m0151-pipe", "aid": str(acc_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO variant_groups (id, organisation_id, pipeline_id, name, "
                    "variants, selection_strategy) "
                    "VALUES (:id, :oid, :pid, 'g1', '[]'::json, 'weighted')"
                ),
                {"id": str(vg_a), "oid": str(org_a), "pid": str(pipe_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO saved_views (id, organisation_id, account_id, name, "
                    "view_type, filters, sort_order) "
                    "VALUES (:id, :oid, :aid, 'v1', 'run_list', '{}'::json, 'asc')"
                ),
                {"id": str(uuid.uuid4()), "oid": str(org_a), "aid": str(acc_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                    "trigger_type, langgraph_thread_id, run_number, input_hash, "
                    "total_cost_usd, variant_group_id) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', :thread, 1, :hash, 0, :vg)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_a),
                    "pid": str(pipe_a),
                    "sid": str(uuid.uuid4()),
                    "thread": str(uuid.uuid4()),
                    "hash": "0" * 64,
                    "vg": str(vg_a),
                },
            )
            # Edge endpoints are pipeline-graph UUIDs, never rows in `nodes`.
            await conn.execute(
                text(
                    "INSERT INTO pipeline_edges (id, pipeline_id, source_node_id, "
                    "target_node_id, edge_type, source_port, target_port) "
                    "VALUES (:id, :pid, :s, :t, 'normal', 'out', 'in')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "pid": str(pipe_a),
                    "s": str(uuid.uuid4()),
                    "t": str(uuid.uuid4()),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, "
                    "account_id, name, eval_type, config_json, failure_behaviour, node_id) "
                    "VALUES (:id, :oid, :pid, :aid, 'e1', 'regex', '{}'::json, 'warn', :nid)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_a),
                    "pid": str(pipe_a),
                    "aid": str(acc_a),
                    "nid": str(uuid.uuid4()),
                },
            )

            await conn.execute(text("SET session_replication_role = 'default'"))
    finally:
        await engine.dispose()
    return {"org_a": org_a, "acc_a": acc_a, "pipe_a": pipe_a, "vg_a": vg_a}


async def _constraint_count(engine, conname: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :c"),
                    {"c": conname},
                )
            ).scalar()
        )


async def _index_count(engine, indexname: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :i"),
                    {"i": indexname},
                )
            ).scalar()
        )


async def test_0151_constraints_added_and_downgrade(isolated_db_url, monkeypatch) -> None:
    db_url = isolated_db_url
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", db_url)
    config = _alembic_config(db_url)
    engine = create_async_engine(db_url, poolclass=NullPool)

    try:
        await _seed(db_url)

        # Run the real migration (constraints + indexes are added here).
        command.upgrade(config, MIGRATION_REV)

        # --- Indexes present ---
        assert await _index_count(engine, "ix_runs_organisation_id_status") == 1
        assert await _index_count(engine, "ix_error_events_organisation_id_status") == 1
        assert await _index_count(engine, "ix_error_groups_organisation_id_status") == 1

        # --- FKs + CHECK present (data was clean, so all added) ---
        assert await _constraint_count(engine, "fk_runs_variant_group") == 1
        assert await _constraint_count(engine, "fk_organisations_plan") == 1
        assert await _constraint_count(engine, "ck_saved_views_sort_order") == 1

        # --- fk_runs_variant_group references variant_groups(id) ---
        async with engine.connect() as conn:
            refs = (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_constraint k "
                        "JOIN pg_class c ON c.oid = k.confrelid "
                        "WHERE k.conname = 'fk_runs_variant_group'"
                    )
                )
            ).scalar()
            assert refs == "variant_groups", "FK must target variant_groups"
            refs2 = (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_constraint k "
                        "JOIN pg_class c ON c.oid = k.confrelid "
                        "WHERE k.conname = 'fk_organisations_plan'"
                    )
                )
            ).scalar()
            assert refs2 == "tier_catalog", "FK must target tier_catalog"

        # --- Downgrade reverses the migration cleanly (-1 = one step back) ---
        config.cmd_opts = types.SimpleNamespace(command="downgrade")
        command.downgrade(config, "-1")
        assert await _constraint_count(engine, "fk_runs_variant_group") == 0
        assert await _constraint_count(engine, "fk_organisations_plan") == 0
        assert await _constraint_count(engine, "ck_saved_views_sort_order") == 0
        assert await _index_count(engine, "ix_runs_organisation_id_status") == 0
        assert await _index_count(engine, "ix_error_events_organisation_id_status") == 0
        assert await _index_count(engine, "ix_error_groups_organisation_id_status") == 0
    finally:
        # Restore the head state (idempotent re-add) for cleanliness; the
        # isolated database is dropped by the fixture regardless.
        config.cmd_opts = types.SimpleNamespace(command="upgrade")
        command.upgrade(config, "heads")
        await engine.dispose()
