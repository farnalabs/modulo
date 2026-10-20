"""FAR-2005 — terminal-fail a node-started run at a short dead-heartbeat bound.

FAR-1999 reaps a node-started, zero-progress run at its durable NODE DEADLINE
(~55 min for the Branch Fixer). FAR-2000 showed that is still far too long: the
SAQ executor heartbeats every ``RUN_HEARTBEAT_SECONDS`` (30s) regardless of node
progress, so a stopped heartbeat is evidence the executor is dead long before
any node budget expires.

This module proves the two things the ticket requires:

* a DEAD-heartbeat node-started run IS reaped at the short bound; and
* a LIVE-heartbeat run is UNTOUCHED — including one whose heartbeat is merely
  slow but still inside the bound (the dangerous false-positive case).

The behaviour is proven by EXECUTING the reaping statement against a real
in-memory SQLite database (the predicate uses bound datetimes, not Postgres
``now()`` arithmetic, precisely so it is executable here), not by asserting SQL
text. The mock-based tests then prove the sweep wires the arm in, runs it BEFORE
the legacy arm, and leaves the legacy re-dispatch exclusion byte-for-byte
unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import modulo.core.run_admission as ra
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine.error_codes import map_legacy_code
from modulo.core.run_admission import reconcile_pipeline_slots
from modulo.db.models.base import Base
from modulo.db.models.run import Run

ORG_ID = uuid.UUID("18348064-eca3-4aa7-be96-8f6c9123efd0")
PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000006666")
NOW = datetime(2026, 10, 20, 12, 0, 0, 123456)
BOUND_SECONDS = 300


# ---------------------------------------------------------------------------
# Real-SQLite predicate proof — the dead/live-heartbeat decision itself
# ---------------------------------------------------------------------------


def _seed_run(
    *,
    name: str,
    heartbeat_at: datetime | None,
    started_at: datetime | None = NOW - timedelta(hours=1),
    status: str = "running",
    dispatcher: str | None = "saq",
    dispatch_phase: str | None = PHASE_FIRST_NODE_DISPATCHED,
    guardrail_override_resume_pending: bool = False,
    cancellation_requested: bool = False,
) -> Run:
    return Run(
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        run_number=1,
        input_hash="a" * 64,
        langgraph_thread_id="t-" + uuid.uuid4().hex + name,
        status=status,
        dispatcher=dispatcher,
        dispatch_phase=dispatch_phase,
        dispatch_phase_entered_at=started_at,
        heartbeat_at=heartbeat_at,
        started_at=started_at,
        created_at=started_at,
        claim_count=1,
        # Explicit: the server_default is the STRING "false" on SQLite, which
        # the raw predicate's boolean comparisons would not coerce.
        cancellation_requested=cancellation_requested,
        guardrail_override_resume_pending=guardrail_override_resume_pending,
    )


async def _reap(session: AsyncSession, *, now: datetime, bound_seconds: int) -> list[Any]:
    """Execute the REAL reaping statement and return the rows it reaped.

    ``ORG_ID.hex`` (not the dashed string the sweep binds in production)
    because SQLite stores SQLAlchemy ``Uuid`` columns as 32-char hex; the
    production bind is the same predicate against a native ``uuid`` column.
    """
    result = await session.execute(
        ra._REAP_DEAD_HEARTBEAT_NODE_STARTED_SQL,
        {
            "oid": ORG_ID.hex,
            "saq_dispatcher": "saq",
            "first_node_phase": PHASE_FIRST_NODE_DISPATCHED,
            "dead_heartbeat_cutoff": now - timedelta(seconds=bound_seconds),
            "completed_at": now,
            "detail": ra._DEAD_HEARTBEAT_REAP_DETAIL,
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


def _heartbeat(seconds_ago: int) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


class TestDeadHeartbeatReapPredicate:
    """Execute the statement: only a provably-dead node-started SAQ run matches."""

    async def test_dead_heartbeat_node_started_run_is_reaped(self, db_session: AsyncSession) -> None:
        dead = _seed_run(name="-dead", heartbeat_at=_heartbeat(660))
        db_session.add(dead)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert [row.id for row in reaped] == [dead.id.hex]
        await db_session.refresh(dead)
        assert dead.status == "failed"
        assert dead.error_code == "heartbeat_stale"
        assert dead.completed_at is not None
        assert dead.dispatched_at is None
        assert dead.dispatcher is None
        assert dead.heartbeat_at is None
        assert "executor stopped heartbeating" in (dead.error_detail or "")

    async def test_live_heartbeat_run_is_untouched(self, db_session: AsyncSession) -> None:
        """A live executor's run — the case that MUST NOT regress."""
        live = _seed_run(name="-live", heartbeat_at=_heartbeat(1))
        db_session.add(live)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(live)
        assert live.status == "running"
        assert live.error_code is None
        assert live.dispatcher == "saq"

    async def test_slow_but_in_bound_heartbeat_run_is_untouched(self, db_session: AsyncSession) -> None:
        """A heartbeat write delayed but still INSIDE the bound is not death."""
        slow = _seed_run(name="-slow", heartbeat_at=_heartbeat(BOUND_SECONDS - 30))
        db_session.add(slow)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(slow)
        assert slow.status == "running"

    async def test_heartbeat_exactly_at_the_bound_is_untouched(self, db_session: AsyncSession) -> None:
        """The bound is strict (``< cutoff``): age == bound is not yet evidence."""
        at_bound = _seed_run(name="-at-bound", heartbeat_at=_heartbeat(BOUND_SECONDS))
        db_session.add(at_bound)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(at_bound)
        assert at_bound.status == "running"

    async def test_null_heartbeat_falls_back_to_a_truthful_age_anchor(self, db_session: AsyncSession) -> None:
        """No heartbeat ever written: started_at is the same liveness reference."""
        no_beat = _seed_run(name="-no-beat", heartbeat_at=None, started_at=_heartbeat(660))
        db_session.add(no_beat)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert [row.id for row in reaped] == [no_beat.id.hex]

    async def test_the_same_dead_row_survives_a_wider_bound(self, db_session: AsyncSession) -> None:
        """Sanity: the row is dead by the bound, not matched unconditionally."""
        dead = _seed_run(name="-dead-wide", heartbeat_at=_heartbeat(660))
        db_session.add(dead)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=3600)

        assert reaped == []
        await db_session.refresh(dead)
        assert dead.status == "running"


class TestDeadHeartbeatReapScope:
    """Every narrowing premise is load-bearing; a row outside the class is left."""

    async def test_non_saq_dispatcher_is_untouched(self, db_session: AsyncSession) -> None:
        row = _seed_run(name="-non-saq", heartbeat_at=_heartbeat(660), dispatcher="local")
        db_session.add(row)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(row)
        assert row.status == "running"

    async def test_non_node_started_run_is_untouched(self, db_session: AsyncSession) -> None:
        row = _seed_run(name="-pre-node", heartbeat_at=_heartbeat(660), dispatch_phase=None)
        db_session.add(row)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(row)
        assert row.status == "running"

    async def test_guardrail_override_resume_class_is_untouched(self, db_session: AsyncSession) -> None:
        """FAR-1728 owns this shape with a checkpoint RESUME — a strictly
        better recovery than a terminal fail, and its flip stamps the heartbeat
        into the past by design."""
        row = _seed_run(
            name="-override",
            heartbeat_at=_heartbeat(660),
            guardrail_override_resume_pending=True,
        )
        db_session.add(row)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(row)
        assert row.status == "running"

    async def test_cancellation_requested_run_is_untouched(self, db_session: AsyncSession) -> None:
        row = _seed_run(name="-cancelling", heartbeat_at=_heartbeat(660), cancellation_requested=True)
        db_session.add(row)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(row)
        assert row.status == "running"

    async def test_non_running_status_is_untouched(self, db_session: AsyncSession) -> None:
        row = _seed_run(name="-awaiting", heartbeat_at=_heartbeat(660), status="awaiting_human")
        db_session.add(row)
        await db_session.commit()

        reaped = await _reap(db_session, now=NOW, bound_seconds=BOUND_SECONDS)

        assert reaped == []
        await db_session.refresh(row)
        assert row.status == "awaiting_human"


class TestDeadHeartbeatReapStatementIsTerminalFailOnly:
    """The arm must never re-dispatch: no ``pending`` write, no phase reset."""

    def test_statement_never_writes_pending(self) -> None:
        sql = str(ra._REAP_DEAD_HEARTBEAT_NODE_STARTED_SQL)
        assert "THEN 'pending'" not in sql
        assert "'pending'" not in sql
        assert "status = 'failed'" in sql

    def test_statement_is_scoped_and_rechecks_every_premise(self) -> None:
        sql = str(ra._REAP_DEAD_HEARTBEAT_NODE_STARTED_SQL)
        assert "status = 'running'" in sql
        assert "organisation_id = :oid" in sql
        assert "cancellation_requested = false" in sql
        assert "dispatcher = :saq_dispatcher" in sql
        assert "dispatch_phase = :first_node_phase" in sql
        assert "guardrail_override_resume_pending = false" in sql
        assert "COALESCE(heartbeat_at, started_at, created_at) < :dead_heartbeat_cutoff" in sql

    def test_legacy_redispatch_exclusion_is_unchanged(self) -> None:
        """FAR-1649: the retry-reset arm still excludes node-started rows."""
        sql = str(ra._RECONCILE_RUNS_SQL)
        assert "AND (claim_count > :retry_budget " in sql
        assert "OR dispatch_phase IS NULL " in sql
        assert "OR dispatch_phase <> :first_node_phase)" in sql
        assert "THEN 'pending'" in sql


class TestDeadHeartbeatBound:
    """The chosen bound and the configurable-cadence widening."""

    def test_default_cadence_uses_the_five_minute_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = SimpleNamespace(slot_reconcile_stale_seconds=1800, heartbeat_stale_retry_budget=3)
        settings.run_heartbeat_seconds = 30  # type: ignore[attr-defined]
        monkeypatch.setattr(ra, "get_settings", lambda: settings)

        assert ra.dead_heartbeat_reap_seconds() == 300

    def test_slow_configured_cadence_never_encodes_fewer_than_five_beats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = SimpleNamespace(slot_reconcile_stale_seconds=1800, heartbeat_stale_retry_budget=3)
        settings.run_heartbeat_seconds = 120  # type: ignore[attr-defined]
        monkeypatch.setattr(ra, "get_settings", lambda: settings)

        assert ra.dead_heartbeat_reap_seconds() == ra.DEAD_HEARTBEAT_REAP_MIN_MISSED_BEATS * 120

    def test_a_settings_double_without_the_field_falls_back_safely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # MagicMock exposes every attribute as a non-int mock — the helper must
        # not treat that as a cadence.
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock())

        assert ra.dead_heartbeat_reap_seconds() == ra.DEAD_HEARTBEAT_REAP_FLOOR_SECONDS


class TestReapedCodeIsHarnessVocabulary:
    """A dead worker is not the pipeline's fault — and not a code failure."""

    def test_recorded_code_canonicalizes_to_harness_heartbeat_stale(self) -> None:
        assert map_legacy_code("heartbeat_stale") == "harness.heartbeat_stale"

    def test_recorded_code_is_distinct_from_dispatch_failed_and_worker_lost(self) -> None:
        canonical = map_legacy_code("heartbeat_stale")
        assert canonical != map_legacy_code("worker_lost")
        assert canonical != map_legacy_code("never_dispatched")
        assert canonical.startswith("harness.")


# ---------------------------------------------------------------------------
# Sweep wiring — the arm runs first, counts, and advances the terminalised run
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


def _reaped_row() -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
        claim_count=1,
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
    )


class _SweepConn:
    def __init__(self, statements: list[str], reaped_rows: list[Any], params_seen: list[dict[str, Any]]) -> None:
        self._statements = statements
        self._reaped_rows = reaped_rows
        self.params_seen = params_seen

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        self._statements.append(sql)
        if params is not None:
            self.params_seen.append(dict(params))
        if "SELECT id FROM organisations" in sql:
            return _Result([(ORG_ID,)])
        if ":dead_heartbeat_cutoff" in sql:
            return _Result(list(self._reaped_rows))
        return _Result([])


class _SweepEngine:
    def __init__(self, statements: list[str], reaped_rows: list[Any]) -> None:
        self._statements = statements
        self._reaped_rows = reaped_rows
        self.params_seen: list[dict[str, Any]] = []

    def connect(self) -> _SweepConn:
        return _SweepConn(self._statements, self._reaped_rows, self.params_seen)


class TestSweepWiresTheReapArm:
    async def test_dead_heartbeat_row_is_counted_and_advanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        row = _reaped_row()
        engine = _SweepEngine(statements, [row])
        monkeypatch.setattr(
            ra,
            "get_settings",
            lambda: SimpleNamespace(
                slot_reconcile_stale_seconds=1800,
                heartbeat_stale_retry_budget=3,
                run_heartbeat_seconds=30,
            ),
        )
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance:
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["reaped"] == 1
        assert result["released"] == 0
        assert result["retried"] == 0
        assert result["per_pipeline"] == {str(PIPELINE_ID): 1}
        advance.assert_awaited_once()
        assert advance.await_args.args[1] == row.id
        assert advance.await_args.args[2] == ORG_ID

    async def test_reap_statement_runs_before_the_legacy_statement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        engine = _SweepEngine(statements, [])
        monkeypatch.setattr(
            ra,
            "get_settings",
            lambda: SimpleNamespace(
                slot_reconcile_stale_seconds=1800,
                heartbeat_stale_retry_budget=3,
                run_heartbeat_seconds=30,
            ),
        )
        await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        reap_index = next(i for i, sql in enumerate(statements) if ":dead_heartbeat_cutoff" in sql)
        legacy_index = next(
            i for i, sql in enumerate(statements) if "status = 'running'" in sql and ":stale_seconds" in sql
        )
        assert reap_index < legacy_index

    async def test_reap_binds_the_short_bound_as_datetimes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _SweepEngine([], [])
        monkeypatch.setattr(
            ra,
            "get_settings",
            lambda: SimpleNamespace(
                slot_reconcile_stale_seconds=1800,
                heartbeat_stale_retry_budget=3,
                run_heartbeat_seconds=30,
            ),
        )
        await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        reap_params = [p for p in engine.params_seen if "dead_heartbeat_cutoff" in p]
        assert len(reap_params) == 1
        cutoff = reap_params[0]["dead_heartbeat_cutoff"]
        completed_at = reap_params[0]["completed_at"]
        assert isinstance(cutoff, datetime)
        assert isinstance(completed_at, datetime)
        assert (completed_at - cutoff).total_seconds() == ra.DEAD_HEARTBEAT_REAP_FLOOR_SECONDS

    async def test_non_node_started_returned_rows_are_not_counted_as_reaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defence-in-depth against a row source that omits ``dispatch_phase``."""
        statements: list[str] = []
        engine = _SweepEngine(
            statements, [SimpleNamespace(id=uuid.uuid4(), organisation_id=ORG_ID, pipeline_id=PIPELINE_ID)]
        )
        monkeypatch.setattr(
            ra,
            "get_settings",
            lambda: SimpleNamespace(
                slot_reconcile_stale_seconds=1800,
                heartbeat_stale_retry_budget=3,
                run_heartbeat_seconds=30,
            ),
        )
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance:
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["reaped"] == 0
        advance.assert_not_awaited()
