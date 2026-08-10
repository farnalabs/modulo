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

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import modulo.core.pipeline_execution as pe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


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


async def _insert_run_with_token(
    engine: AsyncEngine,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "running",
    claim_token: str | None = "tok-a",
    cancellation_requested: bool = False,
) -> None:
    run_number = int(run_id.int % 10**9) + 1
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                "run_number, status, claim_token, cancellation_requested) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st, :tok, :cr)"
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
                "tok": claim_token,
                "cr": cancellation_requested,
            },
        )


async def test_transition_run_fenced_and_superseded(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """transition_run: the token-fenced terminal write lands for the owning
    claim and is a no-op for a superseded one (dist/runtime-core A1)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from modulo.db.crud.run import transition_run
    from modulo.db.rls import set_rls_org

    run_id = uuid.uuid4()
    await _insert_run_with_token(
        db_engine,
        run_id=run_id,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        claim_token="tok-owner",
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        ok = await transition_run(
            session,
            run_id,
            test_org,
            target_status="failed",
            error_code="executor_stalled",
            error_detail="boom",
            claim_token="tok-owner",
            allowed_from=frozenset({"running"}),
        )
        assert ok is True

    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        # Superseded token → no-op.
        ok = await transition_run(
            session,
            run_id,
            test_org,
            target_status="failed",
            error_code="executor_stalled",
            claim_token="tok-successor",
            allowed_from=frozenset({"running", "failed"}),
        )
        assert ok is False

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, error_code FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] == "executor_stalled"


async def test_update_run_status_fenced_rewrites_cancel_wins(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """update_run_status with claim_token: a fenced 'complete' write against a
    cancellation-requested row is rewritten to 'cancelled' (B6 CANCEL-WINS)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from modulo.db.crud.run import update_run_status
    from modulo.db.rls import set_rls_org

    run_id = uuid.uuid4()
    await _insert_run_with_token(
        db_engine,
        run_id=run_id,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        claim_token="tok-owner",
        cancellation_requested=True,
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        run = await update_run_status(
            session,
            run_id,
            "complete",
            claim_token="tok-owner",
            total_tokens=10,
            total_cost_usd=0,
        )
        assert run is not None
        assert run.status == "cancelled"

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "cancelled"
