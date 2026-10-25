"""A ``running`` run with no live owner must never keep leaking its slot.

2026-10-22 incident: a deploy restarted the app+worker while ten runs were in
flight. Five sibling runs were terminal-failed promptly by the graceful shutdown
handoff; six stayed ``status='running'`` for 70+ minutes until a human cancelled
them, holding every concurrency slot. Their retained shape was
``dispatcher IS NULL`` + ``dispatched_at IS NULL`` + ``dispatch_phase=
'first_node_dispatched'`` + ``claim_count=2``, and no sweep owned it:

* the legacy ``worker_lost`` arm needs ``claim_count >= 5`` AND a non-NULL
  ``heartbeat_at`` older than the window (``NULL < ...`` is never true);
* ``run_admission._RECONCILE_RUNS_SQL`` excludes a node-started row from its
  retry-reset arm (FAR-1649) and only terminal-fails it once the claim budget is
  exhausted — which cannot happen, because the exclusion means the row is never
  re-claimed, so ``claim_count`` never grows;
* every dispatcher-scoped arm (the FAR-2005 dead-heartbeat reap, the SAQ
  zero-progress terminaliser, ``dispatcher_reconcile``'s RUNNING legs) requires
  ``dispatcher='saq'``.

``pipeline_execution._sweep_org_stale_runs`` now owns that class, writing the
truthful ``harness.worker_lost`` code (the same code the shutdown handoff
stamps) and never re-dispatching (a started node may already have side effects).

Two things are proved here:

* the PREDICATE, by EXECUTING the production statement against a real in-memory
  SQLite database — it binds datetimes instead of using Postgres ``now()``
  arithmetic precisely so it is executable here (the same technique
  ``test_far2005_dead_heartbeat_reap`` uses); and
* the WIRING, by calling the REAL entry point (``stale_run_recovery_sweep``)
  through a connection double that routes that one statement to the live SQLite
  session — so the sweep's own count/report/advance path runs against the row
  the real predicate matched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import modulo.core.pipeline_execution as pe
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine.error_codes import map_legacy_code
from modulo.core.pipeline_execution import stale_run_recovery_sweep
from modulo.db.models.base import Base
from modulo.db.models.run import Run

ORG_ID = uuid.UUID("18348064-eca3-4aa7-be96-8f6c9123efd0")
PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000006666")
NOW = datetime(2026, 10, 22, 5, 10, 0, 123456)
WL_WINDOW = 600


def _seed_run(
    *,
    name: str,
    dispatcher: str | None = None,
    heartbeat_at: datetime | None = None,
    started_at: datetime | None = NOW - timedelta(hours=1),
    status: str = "running",
    dispatch_phase: str | None = PHASE_FIRST_NODE_DISPATCHED,
    claim_count: int = 2,
    run_number: int = 1,
    cancellation_requested: bool = False,
    guardrail_override_resume_pending: bool = False,
) -> Run:
    return Run(
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        run_number=run_number,
        input_hash="a" * 64,
        langgraph_thread_id="t-" + uuid.uuid4().hex + name,
        status=status,
        dispatcher=dispatcher,
        dispatch_phase=dispatch_phase,
        dispatch_phase_entered_at=started_at,
        heartbeat_at=heartbeat_at,
        started_at=started_at,
        created_at=started_at,
        claim_count=claim_count,
        # Explicit: the server_default is the STRING "false" on SQLite, which the
        # raw predicate's boolean comparisons would not coerce.
        cancellation_requested=cancellation_requested,
        guardrail_override_resume_pending=guardrail_override_resume_pending,
    )


async def _sweep_orphans(session: AsyncSession, *, now: datetime = NOW, wl_window: int = WL_WINDOW) -> list[Any]:
    """Execute the REAL orphan statement and return the rows it terminalised.

    ``ORG_ID.hex`` (not the dashed string the sweep binds in production) because
    SQLite stores SQLAlchemy ``Uuid`` columns as 32-char hex; the production bind
    is the same predicate against a native ``uuid`` column.
    """
    result = await session.execute(
        pe._SWEEP_ORPHANED_RUNS_SQL,
        {
            "oid": ORG_ID.hex,
            "first_node_phase": PHASE_FIRST_NODE_DISPATCHED,
            "orphan_cutoff": now - timedelta(seconds=wl_window),
            "completed_at": now,
            "detail": pe._ORPHANED_RUN_DETAIL,
        },
    )
    return list(result.all())


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in {"runs", "organisations"}]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


class TestOrphanPredicate:
    """Execute the statement: only an ownerless, node-started run matches."""

    async def test_no_dispatcher_node_started_run_past_the_window_is_terminalised(
        self, db_session: AsyncSession
    ) -> None:
        orphan = _seed_run(name="-orphan")
        db_session.add(orphan)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert [row[0] for row in swept] == [orphan.id.hex]
        await db_session.refresh(orphan)
        assert orphan.status == "failed"
        assert orphan.error_code == "harness.worker_lost"
        assert orphan.completed_at is not None
        assert orphan.error_detail == pe._ORPHANED_RUN_DETAIL
        # Terminal-fail only: the row must NOT be put back on a re-dispatch path
        # (a started node may already have executed side effects — FAR-1649).
        assert orphan.dispatcher is None
        assert orphan.dispatched_at is None
        assert orphan.heartbeat_at is None

    async def test_live_heartbeat_run_is_not_terminalised(self, db_session: AsyncSession) -> None:
        live = _seed_run(name="-live", heartbeat_at=NOW - timedelta(seconds=30))
        db_session.add(live)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(live)
        assert live.status == "running"

    async def test_run_inside_the_window_is_not_terminalised(self, db_session: AsyncSession) -> None:
        """The grace is the worker-lost window; a younger row is still owned."""
        young = _seed_run(name="-young", started_at=NOW - timedelta(seconds=WL_WINDOW - 60))
        db_session.add(young)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(young)
        assert young.status == "running"

    async def test_saq_owned_run_is_left_to_the_saq_arms(self, db_session: AsyncSession) -> None:
        saq = _seed_run(name="-saq", dispatcher="saq")
        db_session.add(saq)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(saq)
        assert saq.status == "running"

    async def test_foreign_dispatcher_run_is_terminalised(self, db_session: AsyncSession) -> None:
        """``dispatcher != 'saq'`` is the same hole: no arm collects it either."""
        foreign = _seed_run(name="-foreign", dispatcher="legacy")
        db_session.add(foreign)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert [row[0] for row in swept] == [foreign.id.hex]
        await db_session.refresh(foreign)
        assert foreign.status == "failed"

    async def test_nodeless_run_is_left_to_the_reconcile_retry_arm(self, db_session: AsyncSession) -> None:
        """A claimed-but-not-node-started row is re-dispatchable; the reconcile
        retry-reset arm owns it, so this arm must not terminalise it."""
        claimed = _seed_run(name="-claimed", dispatch_phase="claimed")
        db_session.add(claimed)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(claimed)
        assert claimed.status == "running"

    async def test_cancellation_requested_run_is_left_to_the_cancel_path(self, db_session: AsyncSession) -> None:
        cancelled = _seed_run(name="-cancelling", cancellation_requested=True)
        db_session.add(cancelled)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(cancelled)
        assert cancelled.status == "running"

    async def test_guardrail_override_resume_run_is_left_to_its_own_recovery(self, db_session: AsyncSession) -> None:
        """FAR-1728: the override-resume class has a strictly better recovery
        (checkpoint resume), so it must never be reaped here."""
        override = _seed_run(name="-override", guardrail_override_resume_pending=True)
        db_session.add(override)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(override)
        assert override.status == "running"

    async def test_already_terminal_run_is_never_rewritten(self, db_session: AsyncSession) -> None:
        done = _seed_run(name="-done", status="complete")
        db_session.add(done)
        await db_session.commit()

        swept = await _sweep_orphans(db_session)

        assert not swept
        await db_session.refresh(done)
        assert done.error_code is None


def test_recorded_code_is_the_registered_worker_lost_entry() -> None:
    """The arm records ``harness.worker_lost`` — the canonical "the worker died"
    code (never the legacy ``worker_lost`` alias, which means "never dispatched")."""
    assert map_legacy_code("harness.worker_lost") == "harness.worker_lost"
    assert map_legacy_code("worker_lost") != "harness.worker_lost"


class _AsyncResult:
    def __init__(self, rowcount: int = 0, rows: list[Any] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def all(self) -> list[Any]:
        return self._rows


class _SqliteBackedConn:
    """Connection double: the orphan statement runs for REAL on SQLite.

    Every other statement is stubbed (the sibling arms are Postgres-only:
    ``now()``/``interval`` arithmetic and ``set_config``), so this test exercises
    the production entry point's count/report/advance path against the rows the
    production predicate actually matched.
    """

    # A real AsyncConnection exposes ``dialect`` (the row-lock bound reads it); a
    # non-postgres dialect takes the bound's documented no-op branch.
    dialect = SimpleNamespace(name="sqlite")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.orphan_statements = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
        sql = str(stmt)
        if "SELECT id FROM organisations" in sql:
            # ``.hex`` so the sweep's ``str(org_id)`` bind matches SQLite's Uuid
            # storage form (see ``_sweep_orphans``).
            return _AsyncResult(rows=[(ORG_ID.hex,)])
        if "harness.worker_lost" in sql:
            self.orphan_statements += 1
            result = await self._session.execute(stmt, params or {})
            rows = list(result.all())
            return _AsyncResult(rowcount=len(rows), rows=rows)
        return _AsyncResult()


class _Engine:
    def __init__(self, conn: _SqliteBackedConn) -> None:
        self._conn = conn

    def connect(self) -> _SqliteBackedConn:
        return self._conn


class TestSweepEntryPoint:
    """The production sweep (not a private helper) reclaims the leaked shape."""

    async def _run_sweep(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[dict[str, Any], AsyncMock, _SqliteBackedConn]:
        monkeypatch.setattr(
            pe,
            "get_settings",
            lambda: MagicMock(
                saq_never_dispatched_window=300,
                saq_worker_lost_window=WL_WINDOW,
                # FAR-2122: the orphan cutoff defers one reconcile tick past the
                # reconcile stale window (2 * SAQ_JOB_HEARTBEAT).
                saq_job_heartbeat=300,
            ),
        )
        advanced = AsyncMock()
        monkeypatch.setattr(pe, "_advance_terminalised_run", advanced)
        conn = _SqliteBackedConn(db_session)
        result = await stale_run_recovery_sweep(_Engine(conn))  # type: ignore[arg-type]
        return result, advanced, conn

    async def test_sweep_terminalises_the_orphan_and_advances_its_journey(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orphan = _seed_run(name="-swept")
        owned = _seed_run(name="-owned", dispatcher="saq", heartbeat_at=NOW - timedelta(seconds=30), run_number=2)
        db_session.add_all([orphan, owned])
        await db_session.commit()

        result, advanced, conn = await self._run_sweep(db_session, monkeypatch)

        assert conn.orphan_statements == 1
        assert result["worker_orphaned_swept"] == 1
        await db_session.refresh(orphan)
        assert orphan.status == "failed"
        assert orphan.error_code == "harness.worker_lost"
        # The terminalisation ran through the sweep's shared advance path (journeys
        # + daily fact + audit), exactly like every other arm.
        assert advanced.await_count == 1
        assert uuid.UUID(str(advanced.await_args_list[0].args[1])) == orphan.id
        # Advanced as a FAILED terminal run (never as the queue-expiry
        # ``rejected`` supersession).
        assert advanced.await_args_list[0].kwargs.get("status", "failed") == "failed"

    async def test_sweep_defers_to_the_reconcile_resume_arm_for_one_tick(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAR-2122 precedence: the orphan cutoff extends one dispatcher_reconcile
        tick past the reconcile stale window (2*SAQ_JOB_HEARTBEAT = 600s), so the
        checkpoint-resume arm in ``dispatcher_reconcile`` gets a deterministic
        chance to resume a checkpoint-bearing row before this terminaliser acts.

        A row 610s stale is inside the 660s grace: NOT terminalised. A row 720s
        stale is past it: terminalised. Without the grace both are swept on the
        same tick, racing the reconcile arm.
        """
        real_now = datetime.now(UTC)
        inside_grace = _seed_run(
            name="-inside-grace",
            started_at=real_now - timedelta(seconds=WL_WINDOW + 10),
            run_number=1,
        )
        past_grace = _seed_run(
            name="-past-grace",
            started_at=real_now - timedelta(seconds=WL_WINDOW + 120),
            run_number=2,
        )
        db_session.add_all([inside_grace, past_grace])
        await db_session.commit()

        result, _advanced, _conn = await self._run_sweep(db_session, monkeypatch)

        assert result["worker_orphaned_swept"] == 1
        await db_session.refresh(inside_grace)
        await db_session.refresh(past_grace)
        assert inside_grace.status == "running"
        assert past_grace.status == "failed"

    async def test_sweep_leaves_an_owned_run_running(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owned = _seed_run(name="-healthy", dispatcher="saq", heartbeat_at=NOW - timedelta(seconds=30))
        db_session.add(owned)
        await db_session.commit()

        result, advanced, _conn = await self._run_sweep(db_session, monkeypatch)

        assert result["worker_orphaned_swept"] == 0
        advanced.assert_not_awaited()
        await db_session.refresh(owned)
        assert owned.status == "running"
