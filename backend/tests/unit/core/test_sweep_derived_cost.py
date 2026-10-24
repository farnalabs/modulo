"""FAR-2102 — the stale-run sweep books derived sandbox cost for its reaped runs.

The sweep's raw terminal ``UPDATE``s record NO cost columns, so a run that
occupied a sandbox for hours reached the ledger as ``NULL``. A second bulk
terminaliser therefore stayed invisible to every cost report even after
FAR-2077 fixed the worker-shutdown handoff.

What is proved here:

* **arm selection** — only the worker-lost arms are collected for the
  post-commit cost pass; never-dispatched / queue-expired never executed and
  are never priced (:class:`TestCostArmSelection`);
* **wiring** — the production entry point (``stale_run_recovery_sweep``) books
  each reaped worker-lost run through ``finalize_worker_lost_run`` after the
  bulk write committed, and returns the ids it terminalised
  (:class:`TestSweepBooksDerivedCost`);
* **the FAR-2077 gates still hold on this path** — sandbox evidence is
  required, a run that already carries cost is never re-charged, and a booking
  failure is logged while the reaping stays committed
  (:class:`TestSweepBooksDerivedCost`);
* **reaping is independent of the booking pass** — with the pass skipped the
  sweep still terminalises (``test_reaping_survives_without_the_booking_pass``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.pipeline_execution as pe
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_execution import stale_run_recovery_sweep
from modulo.db.crud.run_node_outputs import RunBlobs
from modulo.db.models.base import Base
from modulo.db.models.run import Run

ORG_ID = uuid.UUID("18348064-eca3-4aa7-be96-8f6c9123efd0")
PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000006666")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
STARTED = NOW - timedelta(hours=2, minutes=30)
WL_WINDOW = 600
RATE = Decimal("0.13")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_orphan(
    *,
    name: str,
    run_number: int = 1,
    sandbox_id: str | None = "sbx-1",
    sandbox_dispatch_state: str | None = None,
    started_at: datetime = STARTED,
    total_cost_usd: Decimal | None = None,
    cost_breakdown: list[dict[str, Any]] | None = None,
    status: str = "running",
) -> Run:
    """A node-started, no-dispatcher run — the class the sweep's orphan arm reaps."""
    return Run(
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        run_number=run_number,
        input_hash="a" * 64,
        langgraph_thread_id="t-" + uuid.uuid4().hex + name,
        status=status,
        dispatcher=None,
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
        dispatch_phase_entered_at=started_at,
        heartbeat_at=None,
        started_at=started_at,
        created_at=started_at,
        claim_count=2,
        cancellation_requested=False,
        guardrail_override_resume_pending=False,
        sandbox_id=sandbox_id,
        sandbox_dispatch_state=sandbox_dispatch_state,
        total_cost_usd=total_cost_usd,
        cost_breakdown=cost_breakdown,
    )


@pytest.fixture
async def db_engine() -> AsyncGenerator[Any, None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in {"runs", "organisations"}]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: Any) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        yield session


class _AsyncResult:
    def __init__(self, rowcount: int = 0, rows: list[Any] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def all(self) -> list[Any]:
        return self._rows


class _SqliteBackedConn:
    """Connection double: the orphan statement runs for REAL on SQLite.

    Every other statement is stubbed (the sibling arms are Postgres-only), so
    the sweep's own control flow runs against the rows the production predicate
    actually matched. The orphan UPDATE is COMMITTED immediately: the
    derived-cost pass opens its OWN session after the sweep transaction, and a
    separate connection only sees committed rows.
    """

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
            # storage form (see ``test_stale_sweep_dispatcherless_orphans``).
            return _AsyncResult(rows=[(ORG_ID.hex,)])
        if "harness.worker_lost" in sql:
            self.orphan_statements += 1
            result = await self._session.execute(stmt, params or {})
            rows = list(result.all())
            await self._session.commit()
            return _AsyncResult(rowcount=len(rows), rows=rows)
        return _AsyncResult()


class _Engine:
    def __init__(self, conn: _SqliteBackedConn) -> None:
        self._conn = conn

    def connect(self) -> _SqliteBackedConn:
        return self._conn


def _settings() -> MagicMock:
    return MagicMock(saq_never_dispatched_window=300, saq_worker_lost_window=WL_WINDOW)


def _patch_cost_writes() -> Any:
    """The FAR-2077 finalize internals, mocked at the write boundary."""
    return (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=RATE),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    )


async def _run_sweep(
    db_session: AsyncSession,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], AsyncMock]:
    """Run the production sweep with the derived-cost pass bound to the test DB."""
    monkeypatch.setattr(pe, "get_settings", _settings)
    advanced = AsyncMock()
    monkeypatch.setattr(pe, "_advance_terminalised_run", advanced)
    # ``_attribute_swept_run_derived_cost`` opens its session through the
    # module-level ``async_sessionmaker``; bind it to the fixture engine so the
    # REAL booking path (not a stub) runs against the reaped rows.
    monkeypatch.setattr(
        pe, "async_sessionmaker", lambda *a, **k: async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
    )
    conn = _SqliteBackedConn(db_session)
    result = await stale_run_recovery_sweep(_Engine(conn))  # type: ignore[arg-type]
    return result, advanced


# ---------------------------------------------------------------------------
# Arm selection — only worker-lost rows enter the cost pass
# ---------------------------------------------------------------------------


class _ArmConn:
    """Fake conn returning one row per SQL arm, keyed by a substring of its text."""

    dialect = SimpleNamespace(name="sqlite")

    def __init__(self) -> None:
        self.never_id = uuid.uuid4()
        self.lost_id = uuid.uuid4()
        self.queue_id = uuid.uuid4()
        self.orphan_id = uuid.uuid4()

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
        sql = str(stmt)
        if "error_code = 'never_dispatched'" in sql:
            return _AsyncResult(rowcount=1, rows=[(self.never_id,)])
        if "error_code = 'worker_lost'" in sql:
            return _AsyncResult(rowcount=1, rows=[(self.lost_id,)])
        if "status = 'rejected', error_code = 'queue.expired'" in sql:
            return _AsyncResult(rowcount=1, rows=[(self.queue_id,)])
        if "harness.worker_lost" in sql:
            return _AsyncResult(rowcount=1, rows=[(self.orphan_id,)])
        return _AsyncResult()


class TestCostArmSelection:
    async def test_only_worker_lost_arms_are_collected_for_the_cost_pass(self) -> None:
        """never-dispatched / queue-expired never executed — never priced."""
        conn = _ArmConn()
        terminalised: list[tuple[uuid.UUID, uuid.UUID]] = []
        queue_expired: list[tuple[uuid.UUID, uuid.UUID]] = []
        derived_cost: list[tuple[uuid.UUID, uuid.UUID]] = []

        await pe._sweep_org_stale_runs(
            conn,
            org_id=ORG_ID,
            nd_window=300,
            wl_window=WL_WINDOW,
            stranded_rows=[],
            terminalised_run_ids=terminalised,
            queue_expired_run_ids=queue_expired,
            derived_cost_run_ids=derived_cost,
        )

        collected = {run_id for run_id, _ in derived_cost}
        assert collected == {conn.lost_id, conn.orphan_id}
        assert conn.never_id not in collected
        assert conn.queue_id not in collected
        # Both worker-lost arms still reach the ordinary advance path.
        assert {run_id for run_id, _ in terminalised} >= {conn.lost_id, conn.orphan_id, conn.never_id}

    async def test_collector_is_optional(self) -> None:
        """A caller that passes no collector still gets the terminalisation."""
        conn = _ArmConn()
        terminalised: list[tuple[uuid.UUID, uuid.UUID]] = []

        never, _queue, lost = await pe._sweep_org_stale_runs(
            conn,
            org_id=ORG_ID,
            nd_window=300,
            wl_window=WL_WINDOW,
            stranded_rows=[],
            terminalised_run_ids=terminalised,
            queue_expired_run_ids=[],
        )

        assert (never, lost) == (1, 1)
        assert {run_id for run_id, _ in terminalised} >= {conn.lost_id, conn.orphan_id}


# ---------------------------------------------------------------------------
# The production sweep books the FAR-2077 derived cost post-commit
# ---------------------------------------------------------------------------


class TestSweepBooksDerivedCost:
    async def test_evidenced_run_is_charged_and_unevidenced_run_is_not(
        self,
        db_session: AsyncSession,
        db_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two reaped orphans: sandbox evidence books cost, its absence withholds it."""
        evidenced = _seed_orphan(name="-evidenced", sandbox_id="sbx-1", started_at=NOW - timedelta(hours=2, minutes=30))
        unevidenced = _seed_orphan(
            name="-unevidenced",
            run_number=2,
            sandbox_id=None,
            sandbox_dispatch_state=None,
            started_at=NOW - timedelta(hours=2, minutes=30),
        )
        db_session.add_all([evidenced, unevidenced])
        await db_session.commit()

        reads = _patch_cost_writes()
        with reads[0], reads[1] as mock_urs, reads[2], reads[3], reads[4]:
            result, _advanced = await _run_sweep(db_session, db_engine, monkeypatch)

        # The sweep terminalised both (reaping is unaffected by the cost pass)...
        assert result["worker_orphaned_swept"] == 2
        assert result["derived_cost_attempted"] == 2
        returned = {run_id for run_id, _ in result["terminalised_run_ids"]}
        assert returned == {str(evidenced.id), str(unevidenced.id)}

        # ...but only the run with sandbox evidence was charged.
        mock_urs.assert_awaited_once()
        await db_session.refresh(evidenced)
        await db_session.refresh(unevidenced)
        elapsed_hours = (evidenced.completed_at - evidenced.started_at).total_seconds() / 3600.0
        assert mock_urs.await_args.kwargs["total_cost_usd"] == Decimal(str(elapsed_hours)) * RATE
        # ``update_run_status(session, run_id, status, ...)`` — status positional.
        assert mock_urs.await_args.args[2] == "failed"

    async def test_reaped_ids_are_json_safe_pairs(
        self,
        db_session: AsyncSession,
        db_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The result is persisted to Redis as JSON (``/healthz/ready`` stats)."""
        import json

        orphan = _seed_orphan(name="-json", sandbox_id="sbx-1")
        db_session.add(orphan)
        await db_session.commit()

        reads = _patch_cost_writes()
        with reads[0], reads[1], reads[2], reads[3], reads[4]:
            result, _advanced = await _run_sweep(db_session, db_engine, monkeypatch)

        # No TypeError: the ids travel as string pairs, not UUID objects.
        assert json.loads(json.dumps(result))["terminalised_run_ids"] == [[str(orphan.id), str(ORG_ID)]]

    async def test_run_already_carrying_cost_is_left_untouched(
        self,
        db_session: AsyncSession,
        db_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The double-booking guard: an attributed run is never charged again."""
        already = _seed_orphan(
            name="-costed",
            sandbox_id="sbx-1",
            total_cost_usd=Decimal("0.40"),
            cost_breakdown=[{"component": "sandbox_infra"}],
        )
        db_session.add(already)
        await db_session.commit()

        reads = _patch_cost_writes()
        with reads[0], reads[1] as mock_urs, reads[2], reads[3], reads[4]:
            result, _advanced = await _run_sweep(db_session, db_engine, monkeypatch)

        assert result["worker_orphaned_swept"] == 1
        mock_urs.assert_not_awaited()
        await db_session.refresh(already)
        assert already.total_cost_usd == Decimal("0.40")

    async def test_booking_failure_is_logged_and_the_reap_stays_committed(
        self,
        db_session: AsyncSession,
        db_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A booking failure must never undo or fail the already-committed reap."""
        orphan = _seed_orphan(name="-boom", sandbox_id="sbx-1")
        db_session.add(orphan)
        await db_session.commit()

        with (
            patch(
                "modulo.core.cost_controller.finalize.finalize_worker_lost_run",
                new=AsyncMock(side_effect=RuntimeError("booking exploded")),
            ),
            caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_execution"),
        ):
            result, _advanced = await _run_sweep(db_session, db_engine, monkeypatch)

        assert result["worker_orphaned_swept"] == 1
        assert "swept_cost_record_failed" in caplog.text
        await db_session.refresh(orphan)
        assert orphan.status == "failed"
        assert orphan.error_code == "harness.worker_lost"

    async def test_reaping_survives_without_the_booking_pass(
        self,
        db_session: AsyncSession,
        db_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reaping is independent of the booking pass — skip it and the reap holds."""
        orphan = _seed_orphan(name="-nobooking", sandbox_id="sbx-1")
        db_session.add(orphan)
        await db_session.commit()

        booking = AsyncMock()
        monkeypatch.setattr(pe, "_attribute_swept_run_derived_cost", booking)
        monkeypatch.setattr(pe, "get_settings", _settings)
        advanced = AsyncMock()
        monkeypatch.setattr(pe, "_advance_terminalised_run", advanced)

        conn = _SqliteBackedConn(db_session)
        result = await stale_run_recovery_sweep(_Engine(conn))  # type: ignore[arg-type]

        assert result["worker_orphaned_swept"] == 1
        assert result["derived_cost_attempted"] == 1
        booking.assert_awaited_once()
        await db_session.refresh(orphan)
        assert orphan.status == "failed"
        assert uuid.UUID(str(orphan.id)) == uuid.UUID(str(result["terminalised_run_ids"][0][0]))

    async def test_per_tick_cap_bounds_the_booking_pass(
        self,
        db_session: AsyncSession,
        db_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A large backlog cannot blow the 120s cron budget — overflow is loud."""
        monkeypatch.setattr(pe, "_SWEEP_DERIVED_COST_MAX_PER_TICK", 1)
        first = _seed_orphan(name="-cap1", sandbox_id="sbx-1", run_number=1)
        second = _seed_orphan(name="-cap2", sandbox_id="sbx-2", run_number=2)
        db_session.add_all([first, second])
        await db_session.commit()

        booking = AsyncMock()
        monkeypatch.setattr(pe, "_attribute_swept_run_derived_cost", booking)
        monkeypatch.setattr(pe, "get_settings", _settings)
        monkeypatch.setattr(pe, "_advance_terminalised_run", AsyncMock())

        conn = _SqliteBackedConn(db_session)
        result = await stale_run_recovery_sweep(_Engine(conn))  # type: ignore[arg-type]

        assert result["worker_orphaned_swept"] == 2
        assert result["derived_cost_attempted"] == 1
        assert result["derived_cost_deferred"] == 1
        assert booking.await_count == 1
