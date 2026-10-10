"""FAR-1625: N concurrent run triggers for ONE pipeline must ALL succeed.

Reproduced on prod 2026-10-09 20:08Z: 12 simultaneous ``POST /api/v1/runs`` for
one pipeline produced 9 accepted runs and 3 HTTP 500s that created NO run — work
silently lost. Every run creation takes a pipeline snapshot with the next
``snapshot_version``; concurrent creators computed the same next version and
collided on ``uq_pipeline_snapshot_version``. The FAR-1287 Part 2 optimistic
retry (3 attempts) could not absorb a 12-way burst because the creators kept
reading the same max.

FAR-1625 serialises the version read+insert with a TRANSACTION-scoped advisory
lock on the caller's session (``pg_advisory_xact_lock``, held until the caller
commits), so a competing creator blocks until the previous creator's row is
committed and visible.

These tests run against a REAL Postgres and fire N concurrent
``create_snapshot_from_live_graph`` calls, each on its OWN session/connection,
for a single pipeline, and assert every caller succeeds with a distinct,
contiguous version. They are the discriminating tests: against the pre-FAR-1625
implementation the burst exhausts the retry and the gather raises.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline_snapshot import (
    _snapshot_allocation_lock_keys,
    create_snapshot_from_live_graph,
)
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
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
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def _create_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> PipelineSnapshot:
    """One creator: its OWN session + transaction, exactly like a run-start route."""
    async with _session_factory(engine)() as session, session.begin():
        await set_rls_org(session, org_id)
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
        assert isinstance(snapshot, PipelineSnapshot)
        return snapshot


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


async def _allocation_lock_count(engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    key1, key2 = _snapshot_allocation_lock_keys(pipeline_id)
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :k1 AND objid = :k2"),
                    {"k1": key1 & 0xFFFFFFFF, "k2": key2 & 0xFFFFFFFF},
                )
            ).scalar_one()
        )


async def test_twelve_concurrent_creators_all_succeed_with_distinct_versions(
    concurrent_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1625: the reproduced 12-way burst must produce 12 snapshots and 0
    failures.

    Every caller runs concurrently on its own connection; the allocation lock
    serialises the version read+insert, so each reads the committed max and
    lands on the next version. No caller sees ``IntegrityError`` /
    ``SnapshotVersionAllocationError`` — the un-raised ``gather`` is the
    assertion.
    """
    pipeline_id = await _insert_pipeline(concurrent_engine, test_org, test_user)
    try:
        snapshots = await asyncio.gather(
            *(_create_snapshot(concurrent_engine, test_org, pipeline_id) for _ in range(_CONCURRENT_CREATORS))
        )

        # Every caller returned a snapshot; ids are distinct.
        assert len(snapshots) == _CONCURRENT_CREATORS
        snapshot_ids = {snapshot.id for snapshot in snapshots}
        assert len(snapshot_ids) == _CONCURRENT_CREATORS

        # Versions are unique AND contiguous from 1 — no duplicate (two creators
        # on the same max) and no gap (a silently lost allocation).
        versions = sorted(snapshot.snapshot_version for snapshot in snapshots)
        assert versions == list(range(1, _CONCURRENT_CREATORS + 1))

        # Every row is actually persisted (a lost insert would not be).
        assert await _persisted_snapshot_count(concurrent_engine, pipeline_id) == _CONCURRENT_CREATORS

        # The burst leaves no advisory lock behind.
        assert await _allocation_lock_count(concurrent_engine, pipeline_id) == 0
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
            *(_create_snapshot(concurrent_engine, test_org, pipeline_id) for _ in range(_CONCURRENT_CREATORS))
        )
        assert max(snapshot.snapshot_version for snapshot in burst) == _CONCURRENT_CREATORS

        after = await _create_snapshot(concurrent_engine, test_org, pipeline_id)
        assert after.snapshot_version == _CONCURRENT_CREATORS + 1
    finally:
        await _cleanup(concurrent_engine, pipeline_id)
