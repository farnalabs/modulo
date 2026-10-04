"""FAR-1287 Part 1: a failed snapshot generation must not leak the advisory lock.

The defect
----------
``create_snapshot_from_live_graph`` used to acquire its SESSION-scoped
``pg_try_advisory_lock`` on the CALLER's session and issue the matching
``pg_advisory_unlock`` in the same ``finally`` — also on the caller's session.
When the copy failed with a DB-level error (the ``ProgrammingError`` at the
snapshot-version read, an ``IntegrityError`` at ``flush``) the caller's
transaction was already aborted, so the unlock was rejected (SQLSTATE 25P02 /
SQLAlchemy ``PendingRollbackError``). Because the lock is session-scoped,
ROLLBACK does not release it either: the pooled connection returned to the pool
STILL HOLDING IT, every later ``pg_try_advisory_lock`` returned false, and
snapshot creation failed permanently (``snapshot_lock_busy`` — the HTTP 503
"Pipeline snapshot lock unavailable after 5 attempts" seen on app.modulo.run).

These tests are the only ones that can catch it, because they run against a
REAL Postgres with a POOLED engine:

* ``db_engine`` in the integration conftest is NullPool — its ``close()``
  physically ends the PG session, which releases every advisory lock, so a
  leaky implementation would be cleaned up by accident and these assertions
  would pass against broken code. Production (``db.session._build_engine``)
  uses the default ``AsyncAdaptedQueuePool``, where ``close()`` only hands the
  connection back to the pool — so ``pooled_engine`` below mirrors production.
* Postgres records the two int4 keys in ``pg_locks.classid``/``objid`` as a
  uint32 BIT-CAST of the signed values (verified: ``pg_try_advisory_lock(
  -123456789, ...)`` lands at ``classid = 4171510507`` = ``-123456789 &
  0xFFFFFFFF``; binding the signed value against ``classid`` fails with
  "value out of uint32 range"), so the helpers below mask the keys before
  comparing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline_snapshot import _pipeline_lock_keys, create_snapshot_from_live_graph
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def pooled_engine(migrated_db_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """A POOLED engine on the CURRENT event loop, mirroring production.

    Function-scoped (never session-scoped) so the pooled connections are never
    reused across event loops — see the module docstring for why pooling is the
    whole point of these tests.
    """
    engine = create_async_engine(migrated_db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)


async def _insert_pipeline(engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed pipelines row owned by this test (never a shared fixture)."""
    pipeline_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'manual_approval')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "name": f"snapshot-lock-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _cleanup(engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def _advisory_lock_count(engine: AsyncEngine, key1: int, key2: int) -> int:
    """How many advisory locks for these pipeline keys are held RIGHT NOW.

    Counts across every backend, so it sees a lock held by the caller's
    connection, by the dedicated lock connection, or by a pooled connection that
    still holds it after its owner gave up on it.
    """
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :k1 AND objid = :k2"),
                    {"k1": key1 & 0xFFFFFFFF, "k2": key2 & 0xFFFFFFFF},
                )
            ).scalar_one()
        )


async def _run_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> PipelineSnapshot | None:
    """One caller: its own session + transaction, exactly like a run-start route."""
    async with _session_factory(engine)() as session, session.begin():
        await set_rls_org(session, org_id)
        return await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)


# ---------------------------------------------------------------------------
# (a) a DB-level failure inside the body leaves ZERO advisory locks
# ---------------------------------------------------------------------------
async def test_body_db_failure_leaves_no_advisory_lock(
    pooled_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1287: a real Postgres error inside the copy must not strand the lock.

    The injected failure is executed on the caller's session, so it ABORTS that
    transaction — the exact condition under which the old code's
    ``pg_advisory_unlock`` was rejected with SQLSTATE 25P02 while the
    session-scoped lock survived. The assertion runs while the caller still
    holds its (aborted) connection, so a leaked lock cannot hide behind the
    connection being returned to the pool.
    """
    pipeline_id = await _insert_pipeline(pooled_engine, test_org, test_user)
    key1, key2 = _pipeline_lock_keys(pipeline_id)

    async def _db_level_failure(session: AsyncSession, pipeline: object) -> list[dict[str, object]] | None:
        await session.execute(text("SELECT * FROM table_that_definitely_does_not_exist"))
        return None

    factory = _session_factory(pooled_engine)

    async def _failing_snapshot() -> None:
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    try:
        with (
            patch("modulo.db.crud.pipeline_snapshot._load_guardrail_pins", new=_db_level_failure),
            pytest.raises(ProgrammingError),
        ):
            await _failing_snapshot()
        # The caller's connection has been returned to the POOLED engine here —
        # a leaked session-scoped lock would still be visible in pg_locks.
        assert await _advisory_lock_count(pooled_engine, key1, key2) == 0
    finally:
        await _cleanup(pooled_engine, pipeline_id)


# ---------------------------------------------------------------------------
# (b) the lock is never acquired on the caller's session
# ---------------------------------------------------------------------------
async def test_lock_is_never_held_by_the_callers_session(
    pooled_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1287: mid-copy, the caller's backend holds NO advisory lock — some
    OTHER backend does (the dedicated lock connection), which is what still
    serialises concurrent snapshot creation."""
    pipeline_id = await _insert_pipeline(pooled_engine, test_org, test_user)
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    observed: dict[str, int] = {}

    async def _observe_lock_state(session: AsyncSession, pipeline: object) -> None:
        caller_pid = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        caller_locks = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = :pid"),
                    {"pid": caller_pid},
                )
            ).scalar_one()
        )
        holders = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :k1 AND objid = :k2"),
                    {"k1": key1 & 0xFFFFFFFF, "k2": key2 & 0xFFFFFFFF},
                )
            ).scalar_one()
        )
        observed["caller_locks"] = caller_locks
        observed["holders"] = holders

    try:
        with patch("modulo.db.crud.pipeline_snapshot._load_guardrail_pins", new=_observe_lock_state):
            snapshot = await _run_snapshot(pooled_engine, test_org, pipeline_id)
        assert isinstance(snapshot, PipelineSnapshot)
        assert observed["caller_locks"] == 0
        assert observed["holders"] >= 1
    finally:
        await _cleanup(pooled_engine, pipeline_id)


# ---------------------------------------------------------------------------
# (c) concurrent same-pipeline creation still serialises inside the retry budget
# ---------------------------------------------------------------------------
async def test_concurrent_snapshot_creation_serialises_within_retry_budget(
    pooled_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1287/FAR-527: moving the lock to a dedicated connection must not cost
    the serialisation it exists for.

    The patched body sleeps long enough that, with a working lock, the second
    caller can only enter AFTER the first leaves (peak concurrency 1); with no
    lock at all both bodies would overlap (peak 2) and the assertion fails. Both
    callers still finish inside the 5-attempt / 0.25s retry budget.
    """
    pipeline_id = await _insert_pipeline(pooled_engine, test_org, test_user)
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    in_flight = 0
    peak = 0

    async def _slow_body(session: AsyncSession, pipeline: object) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # Literal duration on purpose (a computed one would sleep longer
            # than the lock's whole retry budget and flake this test).
            await asyncio.sleep(0.2)
        finally:
            in_flight -= 1

    try:
        with patch("modulo.db.crud.pipeline_snapshot._load_guardrail_pins", new=_slow_body):
            first, second = await asyncio.gather(
                _run_snapshot(pooled_engine, test_org, pipeline_id),
                _run_snapshot(pooled_engine, test_org, pipeline_id),
            )
        assert isinstance(first, PipelineSnapshot)
        assert isinstance(second, PipelineSnapshot)
        # Serialised: the two snapshots took distinct versions instead of racing
        # onto the same one.
        assert {first.snapshot_version, second.snapshot_version} == {1, 2}
        assert peak == 1
        assert await _advisory_lock_count(pooled_engine, key1, key2) == 0
    finally:
        await _cleanup(pooled_engine, pipeline_id)


# ---------------------------------------------------------------------------
# the lock connection must not be stranded by asyncio.CancelledError
# ---------------------------------------------------------------------------
async def test_cancelled_snapshot_releases_the_advisory_lock(
    pooled_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1287: cancelling a run-start mid-copy must still release the lock.

    The lock is proven held while the body hangs (count == 1) and gone once the
    task unwinds. The caller's connection goes back to the POOLED engine here,
    so a close-only release that left the physical session holding the lock
    would show 1 after cancellation.
    """
    pipeline_id = await _insert_pipeline(pooled_engine, test_org, test_user)
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    body_entered = asyncio.Event()

    async def _hang_forever(session: AsyncSession, pipeline: object) -> None:
        body_entered.set()
        await asyncio.sleep(30)

    try:
        with patch("modulo.db.crud.pipeline_snapshot._load_guardrail_pins", new=_hang_forever):
            task = asyncio.create_task(_run_snapshot(pooled_engine, test_org, pipeline_id))
            await asyncio.wait_for(body_entered.wait(), timeout=10)
            # The lock is real and held while the copy is in flight.
            assert await _advisory_lock_count(pooled_engine, key1, key2) == 1

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await _advisory_lock_count(pooled_engine, key1, key2) == 0
    finally:
        await _cleanup(pooled_engine, pipeline_id)
