"""Integration tests for modulo.core.pipeline_execution (real Postgres).

These use the session-scoped Testcontainers Postgres + ``db_engine`` from
``tests/integration/conftest.py`` and are marked ``integration`` so they are
excluded from the fast unit suite.

The async tests run on the SESSION event loop (matching the conftest's
``asyncio_default_fixture_loop_scope = "session"``) so the session-scoped
``db_engine`` is used entirely on one loop — creating per-test async engines on
Windows (Proactor) leaks unclosed socket transports that emit unraisable
warnings at shutdown.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import NullPool, create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine

import modulo.core.pipeline_execution as pe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


def _sync_engine(migrated_db_url: str) -> create_engine:
    # NullPool so connections created inside asyncio.to_thread close immediately
    # and never leave unclosed sockets behind.
    return create_engine(
        migrated_db_url.replace("+asyncpg", "+psycopg"),
        poolclass=NullPool,
        pool_pre_ping=True,
    )


async def _insert_run(
    engine: AsyncEngine,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "pending",
) -> None:
    # run_number must be unique per (org, run_number) — derive it from the
    # unique run_id so parallel/serial tests never collide.
    run_number = int(run_id.int % 10**9) + 1
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                "run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
                "st": status,
            },
        )


async def _claim_status(engine: AsyncEngine, run_id: uuid.UUID) -> str:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT status FROM runs WHERE id=:rid"), {"rid": str(run_id)})).fetchone()
        return str(row[0]) if row else ""


async def test_two_concurrent_claims_exactly_one_wins(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    run_id = uuid.uuid4()
    await _insert_run(db_engine, run_id=run_id, org_id=test_org, pipeline_id=test_pipeline, snapshot_id=test_snapshot)

    engine = _sync_engine(migrated_db_url)
    try:
        results = await asyncio.gather(
            asyncio.to_thread(pe.claim_run, engine, str(run_id), str(test_org), 450),
            asyncio.to_thread(pe.claim_run, engine, str(run_id), str(test_org), 450),
        )
    finally:
        engine.dispose()

    assert sum(1 for r in results if r) == 1
    assert await _claim_status(db_engine, run_id) == "running"


async def test_live_heartbeat_claim_fails_and_stale_claim_succeeds(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    run_id = uuid.uuid4()
    await _insert_run(db_engine, run_id=run_id, org_id=test_org, pipeline_id=test_pipeline, snapshot_id=test_snapshot)

    engine = _sync_engine(migrated_db_url)
    try:
        # First claim: pending -> running, heartbeat now().
        assert pe.claim_run(engine, str(run_id), str(test_org), 450) is True
        # Second claim with a fresh heartbeat must be refused.
        assert pe.claim_run(engine, str(run_id), str(test_org), 450) is False

        # Stale the heartbeat beyond the 450s gate, then claim again.
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        assert pe.claim_run(engine, str(run_id), str(test_org), 450) is True
    finally:
        engine.dispose()


async def test_mark_complete_writes_db_enum_complete(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    run_id = uuid.uuid4()
    await _insert_run(
        db_engine,
        run_id=run_id,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        status="running",
    )

    await pe.mark_complete(db_engine, str(run_id), str(test_org))

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, completed_at FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "complete"
    assert row[1] is not None
