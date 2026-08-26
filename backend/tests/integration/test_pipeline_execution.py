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


# ---------------------------------------------------------------------------
# Claim RLS conformance (dist/runtime-core C3) — real Postgres, non-superuser
# role so the RLS policy ``organisation_id = current_setting(...)`` actually
# filters. claim_run_async must set the org context FIRST or the claim UPDATE
# matches ZERO rows under a NOBYPASSRLS role.
# ---------------------------------------------------------------------------


async def _claim_count(
    engine: AsyncEngine,
    run_id: uuid.UUID,
) -> int:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT claim_count, claim_token FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    return int(row[0])


async def test_claim_run_async_real_pg_rls_concurrent(
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """Two concurrent claims on separate non-superuser RLS connections claim the
    run exactly once: exactly one returns a token, claim_count incremented once.

    The atomic ``UPDATE ... WHERE status='pending'`` is the dedupe — a second
    claimer finds the row already running with a fresh heartbeat and loses.
    Both connections run as the NOBYPASSRLS role (``app_engine`` SET ROLEs on
    checkout) and set_config the org context inside claim_run_async, so the
    RLS policy matches the row instead of silently matching zero (C3).
    """
    run_id = uuid.uuid4()
    await _insert_run(
        db_engine,
        run_id=run_id,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        status="pending",
    )

    token_a, token_b = await asyncio.gather(
        pe.claim_run_async(app_engine, str(run_id), str(test_org)),
        pe.claim_run_async(app_engine, str(run_id), str(test_org)),
    )

    tokens = [t for t in (token_a, token_b) if t is not None]
    assert len(tokens) == 1, f"expected exactly one claim winner, got {tokens}"
    assert await _claim_count(db_engine, run_id) == 1

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "running"


async def test_claim_run_async_rls_scoped_claim_requires_set_config(
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """A raw claim UPDATE executed on a NOBYPASSRLS connection WITHOUT the org
    context (``set_config('app.organisation_id', ...)``) matches ZERO rows.

    This pins the C3 contract: under RLS the claim is scoped to the org the
    connection is configured for — a connection with no org context cannot
    claim anything (fail-closed, never a silent wrong-success).
    """
    run_id = uuid.uuid4()
    await _insert_run(
        db_engine,
        run_id=run_id,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        status="pending",
    )

    claim_sql = pe.build_claim_update(
        _stale_seconds=450,
        _claim_cap=20,
        claim_token="tok-no-context",
    )
    async with app_engine.connect() as conn, conn.begin():
        # NOTE: deliberately NO set_config — the RLS policy must refuse.
        result = await conn.execute(
            claim_sql,
            pe._claim_params(str(run_id), str(test_org), 450, 20, "tok-no-context"),
        )
        claimed = result.fetchone() is not None

    assert claimed is False
    assert await _claim_count(db_engine, run_id) == 0


async def test_claim_resume_run_async_real_pg(
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    migrated_db_url: str,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """The resume claim variant (awaiting_human/claimed) works under real PG +
    RLS (non-superuser): a second immediate resume claim loses (idempotent)."""
    run_id = uuid.uuid4()
    await _insert_run_with_token(
        db_engine,
        run_id=run_id,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        status="awaiting_human",
        claim_token="tok-first",
    )

    token = await pe.claim_resume_run_async(app_engine, str(run_id), str(test_org))
    assert token is not None, "resume claim must succeed under RLS (C3)"
    assert await _claim_count(db_engine, run_id) == 1

    second = await pe.claim_resume_run_async(app_engine, str(run_id), str(test_org))
    assert second is None, "a second resume claim on a fresh-heartbeat running row loses"

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, claim_token FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "running"
    assert row[1] == token
