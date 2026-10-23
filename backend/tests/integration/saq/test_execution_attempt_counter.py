"""Real-Postgres coverage for the FAR-2076 execution-attempt counter.

The unit suite mocks every read of ``runs.execution_attempt_count`` (both
``_load_transient_state`` and ``_read_retry_attempt_state``) and never executes
``bump_execution_attempt_count`` against a database, so a revert to
``node_attempt_count`` — or a bump whose fence is inverted — would stay green.
These tests seed the three counters with DIFFERENT values and assert the
executor reads the durable one, then exercise the bump's real SQL: an increment
on a running row, rejection of a superseded claim token, and rejection of a
capacity-demoted row (which must never spend budget).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.core import pipeline_execution as pe
from modulo.core.pipeline_engine import PipelineExecutor
from modulo.db.soft_delete import register_soft_delete_filter

# The global soft-delete filter is registered by an IMPORT SIDE EFFECT; assert
# the precondition in the module that depends on a plain ORM ``get_run`` read.
register_soft_delete_filter()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    status: str = "running",
    claim_token: str | None = "tok-current",
    claim_count: int = 1,
    node_attempt_count: int = 1,
    execution_attempt_count: int = 0,
    error_code: str | None = None,
) -> uuid.UUID:
    """Seed a pipeline + snapshot + run with DELIBERATELY divergent counters."""
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    pipeline_name = f"far2076-counter-test-{uuid.uuid4().hex[:10]}"

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, graph_nodes_json, "
                    "run_context_defaults, visibility, max_concurrent_runs, retry_policy, deleted_at) "
                    "VALUES (:id, :oid, :uid, :pname, '[]'::json, '{}'::json, 'org', 5, '{}'::jsonb, NULL)"
                ),
                {"id": str(pipeline_id), "oid": str(org_id), "uid": str(account_id), "pname": pipeline_name},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, organisation_id, pipeline_id, snapshot_version, "
                    "account_id, graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                    "model_backend_pins_json, composite_bindings_json, run_context_defaults) "
                    "VALUES (:id, :oid, :pid, 1, :uid, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '{}'::json)"
                ),
                {"id": str(snapshot_id), "oid": str(org_id), "pid": str(pipeline_id), "uid": str(account_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, trigger_type, "
                    "status, input_hash, langgraph_thread_id, run_number, dispatcher, claim_count, "
                    "heartbeat_at, dispatched_at, claim_token, node_attempt_count, execution_attempt_count, "
                    "error_code) "
                    "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', :status, 'hash', :thread, :rn, 'saq', "
                    ":claims, now(), now(), :tok, :node_attempts, :execution_attempts, :error_code)"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "sid": str(snapshot_id),
                    "uid": str(account_id),
                    "status": status,
                    "thread": f"{org_id}:{run_id}",
                    "rn": run_number,
                    "claims": claim_count,
                    "tok": claim_token,
                    "node_attempts": node_attempt_count,
                    "execution_attempts": execution_attempt_count,
                    "error_code": error_code,
                },
            )
    finally:
        await eng.dispose()
    return run_id


async def _read_count(db_engine: AsyncEngine, run_id: uuid.UUID) -> int:
    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            value = (
                await conn.execute(
                    text("SELECT execution_attempt_count FROM runs WHERE id = :rid"), {"rid": str(run_id)}
                )
            ).scalar_one()
        return int(value)
    finally:
        await eng.dispose()


async def _executor(db_engine: AsyncEngine) -> tuple[PipelineExecutor, AsyncEngine]:
    """A real executor whose only seam is the DB session factory."""
    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    executor = object.__new__(PipelineExecutor)
    executor._session_factory = async_sessionmaker(eng, expire_on_commit=False)
    executor._claim_token = "tok-current"
    return executor, eng


async def test_read_retry_attempt_state_reads_the_durable_execution_counter(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """The three counters disagree on purpose: the reader must return the
    execution count (4), not claim_count (9) or node_attempt_count (2)."""
    run_id = await _seed_run(
        db_engine,
        test_org,
        test_user,
        claim_count=9,
        node_attempt_count=2,
        execution_attempt_count=4,
    )
    executor, eng = await _executor(db_engine)
    try:
        count, token, _started = await executor._read_retry_attempt_state(test_org, run_id)
    finally:
        await eng.dispose()

    assert count == 4
    assert token == "tok-current"


async def test_load_transient_state_reads_the_durable_execution_counter(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """Same assertion for the transient path's reader."""
    run_id = await _seed_run(
        db_engine,
        test_org,
        test_user,
        claim_count=9,
        node_attempt_count=2,
        execution_attempt_count=4,
    )
    executor, eng = await _executor(db_engine)
    try:
        count, token, _markers, _cancelled, _key = await executor._load_transient_state(run_id=run_id, org_id=test_org)
    finally:
        await eng.dispose()

    assert count == 4
    assert token == "tok-current"


async def test_bump_increments_a_running_row_once(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    run_id = await _seed_run(db_engine, test_org, test_user, execution_attempt_count=0, claim_token="tok-current")
    executor, eng = await _executor(db_engine)
    try:
        async with executor._session_factory() as session, session.begin():
            counted = await pe.bump_execution_attempt_count(session, run_id, test_org, claim_token="tok-current")
    finally:
        await eng.dispose()

    assert counted is True
    assert await _read_count(db_engine, run_id) == 1


async def test_bump_rejects_a_superseded_claim_token(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """A superseded executor must not spend the successor's budget."""
    run_id = await _seed_run(db_engine, test_org, test_user, execution_attempt_count=2, claim_token="tok-successor")
    executor, eng = await _executor(db_engine)
    try:
        async with executor._session_factory() as session, session.begin():
            counted = await pe.bump_execution_attempt_count(session, run_id, test_org, claim_token="tok-old")
    finally:
        await eng.dispose()

    assert counted is False
    assert await _read_count(db_engine, run_id) == 2


async def test_bump_never_charges_a_capacity_demoted_row(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """A demoted claim never began an execution: status is ``pending``, so the
    fence rejects the bump and the counter stays 0."""
    run_id = await _seed_run(
        db_engine,
        test_org,
        test_user,
        status="pending",
        claim_token="tok-current",
        execution_attempt_count=0,
        error_code="pipeline_capacity",
    )
    executor, eng = await _executor(db_engine)
    try:
        async with executor._session_factory() as session, session.begin():
            counted = await pe.bump_execution_attempt_count(session, run_id, test_org, claim_token="tok-current")
    finally:
        await eng.dispose()

    assert counted is False
    assert await _read_count(db_engine, run_id) == 0
