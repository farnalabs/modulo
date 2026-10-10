"""FAR-1625: N concurrent run triggers for ONE pipeline must ALL succeed.

Reproduced on prod 2026-10-09 20:08Z: 12 simultaneous ``POST /api/v1/runs`` for
one pipeline produced 9 accepted runs and 3 that returned an error and created
NO run — work silently lost. Every run creation takes a pipeline snapshot with
the next ``snapshot_version``, and the Postgres log during the burst shows
concurrent creators computing the same next version and colliding on
``uq_pipeline_snapshot_version``. The FAR-1287 Part 2 optimistic retry (3
attempts) could not absorb a 12-way burst because the creators kept reading the
same max.

FAR-1625 serialises the version read+insert with a TRANSACTION-scoped row lock
on the ``pipelines`` row (``SELECT ... FOR NO KEY UPDATE``), held until the
caller commits, so a competing creator blocks until the previous creator's row
is committed and visible.

These tests run against a REAL Postgres and fire N concurrent creators, each on
its OWN session/connection, for a single pipeline. Each concurrent unit creates
BOTH the snapshot AND the run — matching the real run-start route — and asserts
every caller succeeds with a distinct snapshot version and a distinct RUN id.
They are the discriminating tests: a lost race leaves a caller with an
exception, a duplicate version, or a missing run row, all of which fail an
assertion here.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.crud.row_lock import set_mutation_row_lock_timeout
from modulo.db.crud.run import create_run
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

# The reproduced burst size. The prod incident was 12 simultaneous triggers.
_CONCURRENT_CREATORS = 12


@pytest_asyncio.fixture
async def concurrent_engine(migrated_db_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """A POOLED engine sized for the burst, on the CURRENT event loop.

    Function-scoped so pooled connections never cross event loops. The pool is
    sized ABOVE the burst because each creator holds its own connection while it
    waits on the allocation lock — the test must measure lock serialisation,
    never a pool-checkout timeout.
    """
    engine = create_async_engine(migrated_db_url, pool_size=_CONCURRENT_CREATORS + 4, max_overflow=4)
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
                "name": f"far1625-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _cleanup(engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    # Runs reference both the pipeline and the snapshot; delete them first so
    # the dependent FKs cascade before the parents go.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def _create_snapshot_and_run(
    engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[PipelineSnapshot, Run]:
    """One creator: its OWN session + transaction, matching a run-start route.

    The snapshot AND the run are created in the SAME transaction (as
    ``trigger_run`` does), so the allocation lock is held across both inserts
    and the run's foreign-key ``FOR KEY SHARE`` on ``pipelines`` — the exact
    interleaving that must serialise cleanly.
    """
    async with _session_factory(engine)() as session, session.begin():
        await set_rls_org(session, org_id)
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
        assert isinstance(snapshot, PipelineSnapshot)
        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot.id,
            trigger_type="manual",
            input_payload={},
            account_id=account_id,
        )
        assert isinstance(run, Run)
        return snapshot, run


async def _persisted_snapshot_count(engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pipeline_snapshots WHERE pipeline_id = :pid"),
                    {"pid": str(pipeline_id)},
                )
            ).scalar_one()
        )


async def _persisted_run_count(engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE pipeline_id = :pid"),
                    {"pid": str(pipeline_id)},
                )
            ).scalar_one()
        )


async def test_twelve_concurrent_creators_produce_distinct_runs_and_versions(
    concurrent_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1625: the reproduced 12-way burst must produce 12 runs, 12 snapshots,
    and 0 failures.

    Every caller runs concurrently on its own connection; the allocation row
    lock serialises the version read+insert, so each reads the committed max and
    lands on the next version. No caller sees ``IntegrityError`` /
    ``SnapshotVersionAllocationError`` / a deadlock — the un-raised ``gather`` is
    the assertion, and the distinct RUN ids are the requirement the ticket's
    evidence is stated in.
    """
    pipeline_id = await _insert_pipeline(concurrent_engine, test_org, test_user)
    try:
        pairs = await asyncio.gather(
            *(
                _create_snapshot_and_run(concurrent_engine, test_org, pipeline_id, test_user)
                for _ in range(_CONCURRENT_CREATORS)
            )
        )
        snapshots = [snapshot for snapshot, _ in pairs]
        runs = [run for _, run in pairs]

        # Every caller returned a snapshot AND a run.
        assert len(snapshots) == _CONCURRENT_CREATORS
        assert len(runs) == _CONCURRENT_CREATORS

        # (1) DISTINCT run ids — the requirement: no caller lost its run.
        run_ids = {run.id for run in runs}
        assert len(run_ids) == _CONCURRENT_CREATORS
        assert None not in run_ids

        # (2) distinct snapshot ids AND unique, contiguous versions from 1 — no
        # duplicate (two creators on the same max) and no gap (a silently lost
        # allocation).
        snapshot_ids = {snapshot.id for snapshot in snapshots}
        assert len(snapshot_ids) == _CONCURRENT_CREATORS
        versions = sorted(snapshot.snapshot_version for snapshot in snapshots)
        assert versions == list(range(1, _CONCURRENT_CREATORS + 1))

        # Every run references a snapshot created by the same caller: the run's
        # snapshot_version is unique and consistent with the burst.
        assert {run.snapshot_id for run in runs} == snapshot_ids

        # (3) every row is actually persisted (a lost insert would not be).
        assert await _persisted_snapshot_count(concurrent_engine, pipeline_id) == _CONCURRENT_CREATORS
        assert await _persisted_run_count(concurrent_engine, pipeline_id) == _CONCURRENT_CREATORS
    finally:
        await _cleanup(concurrent_engine, pipeline_id)


async def test_sequential_creators_continue_the_version_sequence(
    concurrent_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1625 (composition): the allocation lock does not corrupt the normal
    sequential path — a run created after the burst continues the sequence at
    the next version, not from 1 again.

    This guards the fix against a regression where the lock is held but the
    committed max is somehow not observed by the next creator.
    """
    pipeline_id = await _insert_pipeline(concurrent_engine, test_org, test_user)
    try:
        burst = await asyncio.gather(
            *(
                _create_snapshot_and_run(concurrent_engine, test_org, pipeline_id, test_user)
                for _ in range(_CONCURRENT_CREATORS)
            )
        )
        assert max(snapshot.snapshot_version for snapshot, _ in burst) == _CONCURRENT_CREATORS

        after_snapshot, after_run = await _create_snapshot_and_run(concurrent_engine, test_org, pipeline_id, test_user)
        assert after_snapshot.snapshot_version == _CONCURRENT_CREATORS + 1
        assert isinstance(after_run, Run)
    finally:
        await _cleanup(concurrent_engine, pipeline_id)


async def test_run_and_edit_paths_do_not_deadlock_on_the_allocation_lock(
    concurrent_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1625 (the lock-order inversion this fix removes): a RUN creator and an
    EDIT-style holder of ``pipelines FOR UPDATE`` must NOT deadlock.

    Lock order under the fix — both take the ``pipelines`` row lock FIRST:

    * RUN  : graph copy -> ``pipelines FOR NO KEY UPDATE`` -> snapshot/run inserts;
    * EDIT : ``pipelines FOR UPDATE`` -> graph copy -> ``pipelines FOR NO KEY
      UPDATE`` (self-reentrant: the row is already held, so no wait).

    The EDIT path (``routes.pipelines._reapply_team_gate_inside_mutation_txn`` /
    ``crud.pipeline_snapshot_versioning``) holds ``FOR UPDATE`` before it calls
    ``create_snapshot_from_live_graph``; the module's own code path is simulated
    here because the test may not import the route module.

    Under the pre-fix ADVISORY allocation lock the orders were advisory-then-row
    (RUN) and row-then-advisory (EDIT) — an ABBA cycle PostgreSQL resolves by
    aborting one transaction (SQLSTATE 40P01). The gather below would then raise
    rather than return two snapshots with distinct versions.
    """
    pipeline_id = await _insert_pipeline(concurrent_engine, test_org, test_user)
    edit_holds_row_lock = asyncio.Event()

    async def _edit_saver() -> PipelineSnapshot:
        async with _session_factory(concurrent_engine)() as session, session.begin():
            await set_rls_org(session, test_org)
            # What _reapply_team_gate_inside_mutation_txn does first.
            await set_mutation_row_lock_timeout(session)
            await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update())
            edit_holds_row_lock.set()
            # Hold the row lock briefly so the run creator is forced to contend
            # with it before the edit path asks for the allocation lock.
            await asyncio.sleep(0.3)
            snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
            assert isinstance(snapshot, PipelineSnapshot)
            return snapshot

    async def _run_creator() -> PipelineSnapshot:
        await edit_holds_row_lock.wait()
        async with _session_factory(concurrent_engine)() as session, session.begin():
            await set_rls_org(session, test_org)
            snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
            assert isinstance(snapshot, PipelineSnapshot)
            return snapshot

    edit_task: asyncio.Task[PipelineSnapshot] | None = None
    run_task: asyncio.Task[PipelineSnapshot] | None = None
    try:
        edit_task = asyncio.create_task(_edit_saver())
        await asyncio.wait_for(edit_holds_row_lock.wait(), timeout=10)
        run_task = asyncio.create_task(_run_creator())
        edit_snapshot, run_snapshot = await asyncio.wait_for(
            asyncio.gather(edit_task, run_task),
            timeout=30,
        )
        # Neither transaction was aborted: both allocations serialised on the
        # pipelines row and produced distinct versions.
        assert isinstance(edit_snapshot, PipelineSnapshot)
        assert isinstance(run_snapshot, PipelineSnapshot)
        assert {edit_snapshot.snapshot_version, run_snapshot.snapshot_version} == {1, 2}
    finally:
        for task in (edit_task, run_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [t for t in (edit_task, run_task) if t is not None and t.cancelled()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _cleanup(concurrent_engine, pipeline_id)
