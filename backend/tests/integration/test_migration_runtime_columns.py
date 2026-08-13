"""Integration tests for migration 0074 — runtime-hardening columns round-trip.

Exercises migration 0074 against a real Postgres (testcontainers):

* downgrade to ``0073_run_node_attempt_count`` so the pre-migration schema is
  present, seed rows that only exist under the OLD schema (a ``NULL``
  ``claim_token`` and a ``waiting_for_lock`` run — the old CHECK allows it),
* upgrade back to ``heads`` (applies 0074),
* assert the claim-token backfill + NOT NULL, the 4 new columns
  (``enqueue_failed_at`` / ``sandbox_dispatch_state`` / ``sandbox_id`` on runs,
  ``decision_payload`` on hitl_claims), the ``waiting_for_lock -> pending``
  backfill, and that the recreated ``ck_runs_status`` rejects new
  ``waiting_for_lock`` inserts.

The shared session-scoped DB is restored to ``heads`` in a ``finally`` so a
failed assertion cannot leave other integration tests on a downgraded schema.
"""

import asyncio
import os
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).parents[2]

_REVISION_BEFORE = "0073_run_node_attempt_count"
_REVISION_AFTER = "0074_runtime_hardening_columns"


def _make_alembic_config() -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _sync_url() -> str:
    from modulo.db.migrations.env import _to_sync_url

    return _to_sync_url(os.environ["DATABASE_URL"])


def _run_migration(config: Config, sync_url: str, revision: str, *, downgrade: bool) -> None:
    """Run upgrade/downgrade through alembic's EnvironmentContext directly.

    env.py's ``run_migrations_online`` fast-paths out via ``_db_is_at_head``
    when the DB version equals the script head — which is ALWAYS true just
    before a downgrade, silently turning ``alembic downgrade`` into a no-op
    (pre-existing bug, reported separately). Running the migration through the
    EnvironmentContext API applies the real revision callables against the
    given connection and honours the target revision, so the round-trip is
    genuinely exercised.
    """
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    script = ScriptDirectory.from_config(config)
    migrate_fn: Callable[..., object] = script._downgrade_revs if downgrade else script._upgrade_revs
    engine = create_engine(sync_url, poolclass=NullPool)
    try:
        with (
            engine.begin() as connection,
            EnvironmentContext(
                config,
                script,
                fn=lambda rev, ctx: migrate_fn(revision, rev),
                as_sql=False,
                starting_rev=None,
                destination_rev=revision,
                tag=None,
            ) as env_ctx,
        ):
            env_ctx.configure(
                connection=connection,
                target_metadata=None,
                dialect_opts={"paramstyle": "named"},
            )
            env_ctx.run_migrations()
    finally:
        engine.dispose()


async def _seed_roundtrip_entities(
    engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a dedicated org/account/pipeline/snapshot for the round-trip.

    The shared session-scoped ``test_org`` owns counter-allocated runs (per-org
    ``run_number`` from migration 0093), so seeding the fixed run_numbers 1/2/3
    into it would collide with the shared counter. A fresh org keeps the
    migration-mechanics test fully isolated.
    """
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": "Roundtrip Org", "slug": f"rt-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(account_id),
                "email": f"rt-{org_id.hex[:8]}@test.local",
                "name": "Roundtrip User",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": "Roundtrip Pipeline",
                "uid": str(account_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return org_id, account_id, pipeline_id, snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str,
    run_number: int,
    claim_token: str | None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, status, "
                "trigger_type, run_number, input_hash, langgraph_thread_id, claim_token) "
                "VALUES (:id, :oid, :pid, :sid, :status, 'manual', :rn, :hash, :thread, :tok)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "status": status,
                "rn": run_number,
                "hash": "0" * 64,
                "thread": f"{org_id}:{run_id}",
                "tok": claim_token,
            },
        )
    return run_id


async def _current_revision(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()


async def test_0074_migration_round_trip(
    migrated_db_url: str,
    db_engine: AsyncEngine,
    non_superuser_role: str,
) -> None:
    config = _make_alembic_config()
    sync_url = _sync_url()
    await asyncio.to_thread(_run_migration, config, sync_url, _REVISION_BEFORE, downgrade=True)

    # Dedicated org: seeding fixed run_numbers into the shared session-scoped
    # test_org would collide with its counter-allocated runs (FAR-168). Request
    # non_superuser_role explicitly so the ALTER DEFAULT PRIVILEGES grants in
    # conftest.py are applied before the upgrade-heads below recreates dropped
    # tables (the fixture ordering must not be implicit).
    org_id, account_id, pipeline_id, snapshot_id = await _seed_roundtrip_entities(db_engine)

    try:
        # Seed rows that only exist under the OLD schema.
        null_token_run = await _seed_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            status="running",
            run_number=1,
            claim_token=None,
        )
        waiting_run = await _seed_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            status="waiting_for_lock",
            run_number=2,
            claim_token="seed-token",
        )

        # Apply 0074.
        await asyncio.to_thread(_run_migration, config, sync_url, "heads", downgrade=False)

        async with db_engine.connect() as conn:
            # claim_token NOT NULL + server_default + backfilled NULL.
            nullability = (
                await conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'runs' "
                        "AND column_name = 'claim_token'"
                    )
                )
            ).scalar_one()
            assert nullability == "NO"

            default_expr = (
                await conn.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'runs' "
                        "AND column_name = 'claim_token'"
                    )
                )
            ).scalar_one()
            assert default_expr is not None and "gen_random_uuid" in default_expr

            backfilled = (
                await conn.execute(text("SELECT claim_token FROM runs WHERE id = :id"), {"id": str(null_token_run)})
            ).scalar_one()
            assert backfilled is not None and backfilled != "seed-token"

            # The 3 new runs columns exist.
            runs_cols = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'runs' AND table_schema = 'public'"
                        )
                    )
                ).fetchall()
            }
            assert {"enqueue_failed_at", "sandbox_dispatch_state", "sandbox_id"} <= runs_cols

            # decision_payload exists on hitl_claims as jsonb.
            hitl_cols = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'hitl_claims' AND table_schema = 'public'"
                        )
                    )
                ).fetchall()
            }
            assert "decision_payload" in hitl_cols
            payload_type = (
                await conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'hitl_claims' "
                        "AND column_name = 'decision_payload'"
                    )
                )
            ).scalar_one()
            assert payload_type == "jsonb"

            # waiting_for_lock backfilled to pending.
            waiting_status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": str(waiting_run)})
            ).scalar_one()
            assert waiting_status == "pending"

            # Recreated ck_runs_status rejects new waiting_for_lock inserts.
            with pytest.raises(DBAPIError):
                await conn.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, status, "
                        "trigger_type, run_number, input_hash, langgraph_thread_id) "
                        "VALUES (:id, :oid, :pid, :sid, 'waiting_for_lock', 'manual', 3, :hash, :thread)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "oid": str(org_id),
                        "pid": str(pipeline_id),
                        "sid": str(snapshot_id),
                        "hash": "0" * 64,
                        "thread": f"{org_id}:rejected-{uuid.uuid4()}",
                    },
                )
    finally:
        # Remove the seeded rows and their dedicated org/account/pipeline/
        # snapshot so they never leak into other integration tests (the shared
        # DB is session-scoped). Order matters for FKs: child runs first, then
        # pipeline_snapshots/pipelines, then the org-scoped account/org.
        async with db_engine.begin() as conn:
            await conn.execute(text("DELETE FROM runs WHERE organisation_id = :id"), {"id": str(org_id)})
            await conn.execute(text("DELETE FROM pipeline_snapshots WHERE id = :id"), {"id": str(snapshot_id)})
            await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
            await conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": str(account_id)})
            await conn.execute(text("DELETE FROM organisations WHERE id = :id"), {"id": str(org_id)})
        # Always restore heads — a failed assertion must not leave the shared
        # session-scoped DB downgraded for the tests that follow.
        if await _current_revision(db_engine) != _REVISION_AFTER:
            await asyncio.to_thread(_run_migration, config, sync_url, "heads", downgrade=False)
