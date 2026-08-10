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
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

BACKEND_ROOT = Path(__file__).parents[2]

_REVISION_BEFORE = "0073_run_node_attempt_count"
_REVISION_AFTER = "0074_runtime_hardening_columns"

pytestmark = pytest.mark.integration


def _make_alembic_config() -> Config:
    from modulo.db.migrations.env import _to_sync_url

    url = os.getenv("DATABASE_URL", "")
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", _to_sync_url(url))
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


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
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    config = _make_alembic_config()
    await asyncio.to_thread(command.downgrade, config, _REVISION_BEFORE)

    seeded_run_ids: list[uuid.UUID] = []
    try:
        # Seed rows that only exist under the OLD schema.
        null_token_run = await _seed_run(
            db_engine,
            org_id=test_org,
            pipeline_id=test_pipeline,
            snapshot_id=test_snapshot,
            status="running",
            run_number=1,
            claim_token=None,
        )
        seeded_run_ids.append(null_token_run)
        waiting_run = await _seed_run(
            db_engine,
            org_id=test_org,
            pipeline_id=test_pipeline,
            snapshot_id=test_snapshot,
            status="waiting_for_lock",
            run_number=2,
            claim_token="seed-token",
        )
        seeded_run_ids.append(waiting_run)

        # Apply 0074.
        await asyncio.to_thread(command.upgrade, config, "heads")

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
                        "oid": str(test_org),
                        "pid": str(test_pipeline),
                        "sid": str(test_snapshot),
                        "hash": "0" * 64,
                        "thread": f"{test_org}:rejected-{uuid.uuid4()}",
                    },
                )
    finally:
        # Remove the seeded rows so they never leak into other integration tests.
        if seeded_run_ids:
            async with db_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM runs WHERE id IN (:a, :b)"),
                    {"a": str(seeded_run_ids[0]), "b": str(seeded_run_ids[1])},
                )
        # Always restore heads — a failed assertion must not leave the shared
        # session-scoped DB downgraded for the tests that follow.
        if await _current_revision(db_engine) != _REVISION_AFTER:
            await asyncio.to_thread(command.upgrade, config, "heads")
