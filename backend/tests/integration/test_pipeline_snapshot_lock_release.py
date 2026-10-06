"""FAR-1287 Parts 1 & 2: a failed snapshot generation must not leak the advisory lock,
and the version-allocation race must not reach a caller.

Part 1 — the defect
-------------------
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

Part 2 (Workstream B) — the version-allocation race
---------------------------------------------------
The lock is released in ``create_snapshot_from_live_graph``'s ``finally``,
i.e. BEFORE the caller's transaction commits. A second creator that takes the
lock in that window reads ``max(snapshot_version)`` WITHOUT seeing the first
creator's uncommitted row, computes the same version, and blocks on the unique
index until the first commits — then fails with ``IntegrityError`` on
``uq_pipeline_snapshot_version``. Part 2 contains that collision in a
SAVEPOINT and retries the allocation. ``test_concurrent_creators_after_lock_
release_succeed_with_distinct_versions`` reproduces the window deterministically
(A's creator holds its transaction open after releasing the lock; B is started
only once A has released) and is the discriminating test: without the retry,
B's ``IntegrityError`` escapes to the caller and the test fails.

Part 2 (Workstream A) — the operator clear path
-----------------------------------------------
``test_operator_snapshot_lock_endpoints_report_then_release`` drives the two
system-admin endpoints against a REAL held lock through the ASGI client, whose
DB session runs as the non-superuser app role — which is exactly where the
``pg_terminate_backend`` privilege question is decided empirically rather than
assumed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_current_user
from modulo.api.main import app
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.pipeline_snapshot import _pipeline_lock_keys, create_snapshot_from_live_graph
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

# How long the Part 2 race test waits for B's backend to show up as BLOCKED in
# pg_locks before declaring the window was not reproduced.
_BLOCKED_WAIT_SECONDS = 10.0


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
    serialises concurrent snapshot creation — and the caller's POOL is not
    consumed by that lock either.

    The pool assertion is the production regression this shape guards: a lock
    drawn from the caller's engine would take a SECOND slot of the main web pool
    (20+10) per snapshot creation, so a burst would push every waiter into the
    30s ``pool_timeout`` and surface as ``sqlalchemy.exc.TimeoutError``. Mid-copy
    exactly one connection of THIS engine is checked out — the caller's own
    session; the lock's connection belongs to the dedicated NullPool engine.
    """
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
        observed["main_pool_checked_out"] = pooled_engine.sync_engine.pool.checkedout()

    try:
        with patch("modulo.db.crud.pipeline_snapshot._load_guardrail_pins", new=_observe_lock_state):
            snapshot = await _run_snapshot(pooled_engine, test_org, pipeline_id)
        assert isinstance(snapshot, PipelineSnapshot)
        assert observed["caller_locks"] == 0
        assert observed["holders"] >= 1
        # The caller's session only — the lock is NOT a second slot on this pool.
        assert observed["main_pool_checked_out"] == 1
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


# ---------------------------------------------------------------------------
# Part 2 (Workstream B): the version-allocation race window, reproduced
# deterministically — and proven handled.
# ---------------------------------------------------------------------------
async def _wait_for_backend_blocked(engine: AsyncEngine, pid: int) -> bool:
    """Poll ``pg_locks`` until *pid* is WAITING (it has an ungranted lock row).

    The wait is OBSERVED, never slept for: B's duplicate-key INSERT blocks on A's
    uncommitted transaction, so the moment a ``NOT granted`` row appears for B's
    backend is the moment the race window is provably open. Returns False after
    ``_BLOCKED_WAIT_SECONDS`` so the caller fails with a precise message instead
    of hanging.
    """
    deadline = asyncio.get_running_loop().time() + _BLOCKED_WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as conn:
            waiting = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted"),
                        {"pid": pid},
                    )
                ).scalar_one()
            )
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_concurrent_creators_after_lock_release_succeed_with_distinct_versions(
    pooled_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1287 Part 2: two same-pipeline creators overlapping in the
    release-before-commit window BOTH succeed, with distinct versions, and the
    unique constraint never reaches a caller.

    Deterministic reproduction of the window:

    1. creator A runs to completion of ``create_snapshot_from_live_graph`` —
       which RELEASES the advisory lock — but its transaction is held open, so
       its row is still invisible to other transactions;
    2. creator B is then started (so the lock is free for it), reads
       ``max(snapshot_version)`` = 0, computes version 1 and blocks on the
       unique index — asserted via B's own ungranted ``pg_locks`` row;
    3. only then is A allowed to commit, so B's insert fails with the duplicate
       key, rolls back to its SAVEPOINT and re-allocates.

    Without the Part 2 retry, step 3 raises ``IntegrityError`` out of B and the
    gather fails — that is the discriminating failure this test exists for.
    """
    pipeline_id = await _insert_pipeline(pooled_engine, test_org, test_user)
    key1, key2 = _pipeline_lock_keys(pipeline_id)

    copy_calls = 0
    b_backend: dict[str, int] = {}
    a_lock_released = asyncio.Event()
    commit_a = asyncio.Event()
    b_in_copy = asyncio.Event()

    async def _observe_copy(session: AsyncSession, pipeline: object) -> None:
        # Called once per creator inside the copy (A first: it starts first and
        # B only starts once A has released the lock).
        nonlocal copy_calls
        copy_calls += 1
        if copy_calls == 2:
            b_backend["pid"] = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            b_in_copy.set()

    async def _creator_a() -> PipelineSnapshot:
        async with _session_factory(pooled_engine)() as session, session.begin():
            await set_rls_org(session, test_org)
            snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
            # Lock released inside the call above; the row is NOT committed yet.
            a_lock_released.set()
            await commit_a.wait()
            return snapshot

    async def _creator_b() -> PipelineSnapshot:
        await a_lock_released.wait()
        return await _run_snapshot(pooled_engine, test_org, pipeline_id)

    task_a: asyncio.Task[PipelineSnapshot] | None = None
    task_b: asyncio.Task[PipelineSnapshot] | None = None
    blocked = False
    try:
        with patch("modulo.db.crud.pipeline_snapshot._load_guardrail_pins", new=_observe_copy):
            task_a = asyncio.create_task(_creator_a())
            await asyncio.wait_for(a_lock_released.wait(), timeout=10)
            task_b = asyncio.create_task(_creator_b())
            try:
                await asyncio.wait_for(b_in_copy.wait(), timeout=10)
                blocked = await _wait_for_backend_blocked(pooled_engine, b_backend["pid"])
            finally:
                # Always let A commit, or B would wait on it forever.
                commit_a.set()
            first, second = await asyncio.gather(task_a, task_b)

        # (1) the race window really was open ...
        assert blocked, "B never reached a blocked duplicate-key INSERT, so the race window was not reproduced"
        # (2) ... and NEITHER caller saw uq_pipeline_snapshot_version: both
        # creations succeeded with distinct versions. The gather above is the
        # assertion that the constraint was never surfaced — without the retry
        # it raises IntegrityError from creator B instead of returning.
        assert isinstance(first, PipelineSnapshot)
        assert isinstance(second, PipelineSnapshot)
        assert {first.snapshot_version, second.snapshot_version} == {1, 2}
        assert await _advisory_lock_count(pooled_engine, key1, key2) == 0
    finally:
        commit_a.set()
        for task in (task_a, task_b):
            if task is not None and not task.done():
                task.cancel()
        pending = [t for t in (task_a, task_b) if t is not None and t.cancelled()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _cleanup(pooled_engine, pipeline_id)


# ---------------------------------------------------------------------------
# Part 2 (Workstream A): the operator clear path, end to end against a real
# held lock through the ASGI client (app role = non-superuser).
# ---------------------------------------------------------------------------
async def _app_role_can_terminate(app_engine: AsyncEngine) -> tuple[str, bool]:
    """(current role, may it terminate other backends?) — observed, not assumed.

    ``pg_terminate_backend`` needs superuser or ``pg_signal_backend``. This runs
    as the app role the HTTP routes themselves use, so the answer here is the
    answer the endpoint will get in production.
    """
    async with app_engine.connect() as conn:
        role = str((await conn.execute(text("SELECT current_user"))).scalar_one())
        allowed = bool(
            (
                await conn.execute(
                    text(
                        "SELECT r.rolsuper OR pg_has_role(r.rolname, 'pg_signal_backend', 'MEMBER') "
                        "FROM pg_roles AS r WHERE r.rolname = current_user"
                    )
                )
            ).scalar_one()
        )
    return (role, allowed)


async def test_operator_snapshot_lock_endpoints_report_then_release(
    integration_client: AsyncClient,
    pooled_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1287 Part 2 (A): a held lock is reported by the diagnostic and
    cleared by the release endpoint — or refused with a typed error naming the
    grant, if the app role lacks the right.

    The capability branch is deliberate: the endpoint must exist and behave
    correctly under BOTH outcomes, and which one production gets is a property of
    the deployed role, not of this code.
    """
    pipeline_id = await _insert_pipeline(pooled_engine, test_org, test_user)
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    role, can_terminate = await _app_role_can_terminate(app_engine)

    # Hold the lock on a dedicated backend, AS THE APP ROLE (like the real
    # dedicated lock connection does).
    holder = await app_engine.connect()
    try:
        acquired = bool(
            (
                await holder.execute(
                    text("SELECT pg_try_advisory_lock(:k1, :k2)"),
                    {"k1": key1, "k2": key2},
                )
            ).scalar_one()
        )
        assert acquired, "the test's holder backend failed to take the advisory lock"
        holder_pid = int((await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        assert await _advisory_lock_count(pooled_engine, key1, key2) == 1

        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="operator",
            organisation_id=test_org,
            account_id=test_user,
            org_role="admin",
            is_system_admin=True,
        )
        base = f"/api/v1/admin/pipelines/{pipeline_id}/snapshot-lock"

        # --- diagnostic: the held lock is visible, with the holder's pid -----
        status_resp = await integration_client.get(base)
        assert status_resp.status_code == 200, status_resp.text
        status_body = status_resp.json()
        assert status_body["held"] is True
        assert holder_pid in [holder["pid"] for holder in status_body["holders"]]

        # --- release -------------------------------------------------------
        release_resp = await integration_client.post(f"{base}/release")
        if can_terminate:
            assert release_resp.status_code == 200, release_resp.text
            payload = release_resp.json()
            assert payload["released"] >= 1
            assert holder_pid in payload["pids"]
            assert await _advisory_lock_count(pooled_engine, key1, key2) == 0

            async with pooled_engine.connect() as conn:
                audit_count = int(
                    (
                        await conn.execute(
                            text(
                                "SELECT count(*) FROM audit_events "
                                "WHERE event_type = :event_type AND resource_id = :pipeline_id "
                                "AND organisation_id = :org_id"
                            ),
                            {
                                "event_type": "pipeline_snapshot_lock_released",
                                "pipeline_id": pipeline_id,
                                "org_id": test_org,
                            },
                        )
                    ).scalar_one()
                )
            assert audit_count == 1, f"expected exactly one release audit event, found {audit_count}"
        else:
            # Observed outcome: the app role may not terminate backends. The
            # endpoint must still exist and fail with the typed error that names
            # the grant (GRANT pg_signal_backend TO "<role>") — never a 500, and
            # nothing may have been terminated.
            assert release_resp.status_code == 403, release_resp.text
            problem = release_resp.json()
            assert problem["type"] == "urn:problem:modulo:forbidden"
            assert "pg_signal_backend" in problem["detail"]
            assert await _advisory_lock_count(pooled_engine, key1, key2) == 1

        # The observed capability is logged (not asserted): which branch this
        # test takes is a property of the deployed database role, and the log
        # line records which one production's role will get.
        _log.info("snapshot_lock_terminate_capability app_role=%s allowed=%s", role, can_terminate)
    finally:
        with suppress(Exception):
            await holder.close()
        await _cleanup(pooled_engine, pipeline_id)
