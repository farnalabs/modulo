"""Unit tests for the FAR-1088 (W2) durable dispatch-phase writer.

The writer records post-claim phase entries (``loading_setup``,
``setup_complete``, ``streaming``, ``first_node_dispatched``) durably on
``runs.dispatch_phase``. Five load-bearing properties are proven here:

1. Single-flight + coalescing (at most one in-flight write, latest wins).
2. Bounded + fail-soft (timeout/error are logged and dropped, never raised).
3. Monotonic (the UPDATE guard rejects a backwards phase; token fence rejects
   a superseded attempt).
4. Tracker wiring — exactly ``DURABLE_PHASES`` are recorded; no clearing.
5. FAR-1714 — every durable write appends a ``dispatch_phase_history``
   timeline entry inside the guarded UPDATE; all four claim statements append
   the ``claimed`` attempt-boundary entry; an abandoned write bumps
   ``runs.dispatch_phase_write_drops`` without ever raising into the run.

Prove-the-fix: without the writer, every test here fails at import/construction
(``DispatchPhaseWriter`` does not exist) or at the recorded-state assertions.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

import modulo.core.pipeline_execution as pe
import modulo.core.saq_worker as sw
from modulo.core.pipeline_engine.event_broker import RunEventBroker
from modulo.core.pipeline_engine.executor import PipelineExecutor, _StreamContext, _StreamState
from modulo.core.pipeline_execution import (
    _DROP_COUNTER_UPDATE_SQL,
    _PHASE_UPDATE_SQL,
    DROP_COUNTER_WRITE_TIMEOUT_SECONDS,
    DURABLE_PHASES,
    PHASE_CLAIMED,
    PHASE_FIRST_NODE_DISPATCHED,
    PHASE_GRAPH_COMPILE,
    PHASE_LOADING_SETUP,
    PHASE_SETUP_COMPLETE,
    PHASE_STREAMING,
    PHASE_WRITE_TIMEOUT_SECONDS,
    DispatchPhaseTracker,
    DispatchPhaseWriter,
)

_RUN_ID = "7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f"
_ORG_ID = "8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
_TOK = "tok-abc"
_LOGGER = "modulo.core.pipeline_execution"


class _FakeResult:
    """Minimal DBAPI-ish result: only ``fetchone`` is used by the writer."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


def _row(
    entered_at: datetime | None,
    phase: str = "claimed",
    token: str = _TOK,
) -> dict[str, Any]:
    return {
        "id": _RUN_ID,
        "dispatch_phase": phase,
        "dispatch_phase_entered_at": entered_at,
        "dispatch_phase_history": None,
        "dispatch_phase_write_drops": 0,
        "claim_token": token,
    }


class _FakeConn:
    def __init__(self, engine: _FakeEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        eng = self._engine
        eng.in_flight += 1
        eng.max_in_flight = max(eng.max_in_flight, eng.in_flight)
        try:
            sql = str(stmt)
            if "set_config" in sql:
                return _FakeResult([])
            p = params or {}
            if "dispatch_phase_write_drops" in sql:
                # FAR-1714 drop-counter bump: additive, token-fence-free,
                # bypasses the phase-write block gate (a bump is attempted
                # AFTER the phase write was abandoned) unless its own
                # failure mode is being simulated.
                if eng.raise_bump_error:
                    raise RuntimeError("simulated drop-counter failure")
                eng.row["dispatch_phase_write_drops"] += 1
                return _FakeResult([])
            eng.write_started.set()
            if eng.block is not None:
                await eng.block.wait()
            if eng.raise_error:
                raise RuntimeError("simulated DB failure")
            eng.attempts.append(dict(p))
            row = eng.row
            now = eng.db_now()
            # Stand-in for the SQL predicate: token fence + monotonic guard.
            ok = row["claim_token"] == p["tok"] and (
                row["dispatch_phase_entered_at"] is None or row["dispatch_phase_entered_at"] <= now
            )
            if ok:
                row["dispatch_phase"] = p["phase"]
                row["dispatch_phase_entered_at"] = now
                # FAR-1714: the SAME guarded UPDATE appends one history
                # entry — a rejected statement appends nothing.
                history = row["dispatch_phase_history"]
                if history is None:
                    history = []
                    row["dispatch_phase_history"] = history
                history.append({"phase": p["phase"], "entered_at": now.isoformat()})
            return _FakeResult([(row["id"],)] if ok else [])
        finally:
            eng.in_flight -= 1


class _FakeEngine:
    """Async-engine stand-in emulating the phase UPDATE's WHERE semantics."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.attempts: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.block: asyncio.Event | None = None
        self.raise_error = False
        self.raise_bump_error = False
        self.write_started = asyncio.Event()

    def db_now(self) -> datetime:
        return datetime.now(UTC)

    def connect(self) -> _FakeConn:
        return _FakeConn(self)


def _writer(engine: _FakeEngine, *, timeout_seconds: float = 1.0) -> DispatchPhaseWriter:
    return DispatchPhaseWriter(
        engine,  # type: ignore[arg-type]
        run_id=_RUN_ID,
        org_id=_ORG_ID,
        claim_token=_TOK,
        timeout_seconds=timeout_seconds,
    )


def _fwd_row() -> dict[str, Any]:
    # A claim floor stamped in the past: forward entries must be accepted.
    return _row(datetime.now(UTC) - timedelta(seconds=30))


class TestPhaseUpdateSql:
    """Property 3 (structural): the UPDATE carries the guard + token fence."""

    def test_update_carries_monotonic_guard(self) -> None:
        sql = str(_PHASE_UPDATE_SQL)
        assert "dispatch_phase_entered_at IS NULL OR dispatch_phase_entered_at <=" in sql

    def test_update_fences_on_claim_token_and_run_id(self) -> None:
        sql = str(_PHASE_UPDATE_SQL)
        assert "claim_token=:tok" in sql
        assert "id=:rid" in sql
        assert "organisation_id=:oid" in sql

    def test_update_never_clears_the_columns(self) -> None:
        """Property 4: the phase UPDATE only sets, never resets to NULL."""
        sql = str(_PHASE_UPDATE_SQL)
        assert "=NULL" not in sql
        assert "= NULL" not in sql


class TestMonotonicGuard:
    """Property 3 (behaviour): a backwards entry is a no-op."""

    @pytest.mark.asyncio
    async def test_forward_phase_entry_lands(self) -> None:
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        writer.record(PHASE_LOADING_SETUP)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_LOADING_SETUP

    @pytest.mark.asyncio
    async def test_backwards_phase_entry_rejected(self) -> None:
        # The stored entry time is NEWER than this attempt's DB-clock
        # transaction start — an out-of-order commit. The guard must make it
        # a no-op so the phase cannot move backwards.
        stored_future = datetime.now(UTC) + timedelta(seconds=30)
        eng = _FakeEngine(_row(stored_future, phase=PHASE_STREAMING))
        writer = _writer(eng)
        writer.record(PHASE_LOADING_SETUP)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_STREAMING
        # The statement was still sent (rowcount 0 == guard rejected it).
        assert eng.attempts

    @pytest.mark.asyncio
    async def test_superseded_claim_token_rejected(self) -> None:
        # A successor re-claim rotated the token; this attempt must not write.
        eng = _FakeEngine(_row(datetime.now(UTC) - timedelta(seconds=1), token="tok-successor"))
        writer = _writer(eng)
        writer.record(PHASE_SETUP_COMPLETE)
        await writer.flush()
        assert eng.row["dispatch_phase"] == "claimed"


class TestFailSoft:
    """Property 2: bounded writes that never raise into the run."""

    @pytest.mark.asyncio
    async def test_db_error_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        eng = _FakeEngine(_fwd_row())
        eng.raise_error = True
        writer = _writer(eng)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            writer.record(PHASE_LOADING_SETUP)
            await writer.flush()  # must not raise
        assert "dispatch_phase.write_failed" in caplog.text
        assert eng.row["dispatch_phase"] == "claimed"

    @pytest.mark.asyncio
    async def test_timeout_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        eng = _FakeEngine(_fwd_row())
        eng.block = asyncio.Event()  # never released: the write hangs
        writer = _writer(eng, timeout_seconds=0.05)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            writer.record(PHASE_STREAMING)
            await writer.flush()  # bounded: returns once the write times out
        assert "dispatch_phase.write_timeout" in caplog.text
        assert eng.row["dispatch_phase"] == "claimed"

    def test_record_without_running_loop_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        """Scheduling outside a running loop is fail-soft, not fatal."""
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            writer.record(PHASE_LOADING_SETUP)
        assert "dispatch_phase.record scheduling failed" in caplog.text

    def test_record_unexpected_error_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-RuntimeError scheduling fault is fail-soft too (generic arm)."""
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)

        def _boom() -> Any:
            raise ValueError("simulated scheduling fault")

        monkeypatch.setattr("modulo.core.pipeline_execution.asyncio.get_running_loop", _boom)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            writer.record(PHASE_LOADING_SETUP)
        assert "dispatch_phase.record scheduling failed" in caplog.text

    @pytest.mark.asyncio
    async def test_flush_without_in_flight_task_is_a_noop(self) -> None:
        """A flush before any record returns immediately (no task, no write)."""
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        assert writer._task is None

        await writer.flush()

        assert writer._task is None
        assert not eng.attempts

    @pytest.mark.asyncio
    async def test_drain_reraises_cancelled_error(self) -> None:
        """A CancelledError from the write is re-raised, not swallowed."""
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)

        async def _cancelled(_phase: str) -> None:
            raise asyncio.CancelledError

        writer._write = _cancelled  # type: ignore[method-assign]
        writer._pending = PHASE_STREAMING
        with pytest.raises(asyncio.CancelledError):
            await writer._drain()

    def test_default_timeout_is_two_seconds(self) -> None:
        assert PHASE_WRITE_TIMEOUT_SECONDS == 2.0


class TestSingleFlightCoalescing:
    """Property 1: at most one in-flight write; the latest phase wins."""

    @pytest.mark.asyncio
    async def test_one_in_flight_and_latest_phase_wins(self) -> None:
        eng = _FakeEngine(_fwd_row())
        gate = asyncio.Event()
        eng.block = gate
        writer = _writer(eng)

        writer.record(PHASE_LOADING_SETUP)
        await asyncio.wait_for(eng.write_started.wait(), timeout=1.0)
        first_task = writer._task
        assert first_task is not None

        # Phase entries while a write is in flight coalesce — no new task,
        # and only the LATEST phase is kept for the follow-up write.
        writer.record(PHASE_SETUP_COMPLETE)
        writer.record(PHASE_STREAMING)
        assert writer._task is first_task
        assert writer._pending == PHASE_STREAMING
        assert not eng.attempts

        gate.set()
        await writer.flush()

        # Never more than one write (connection use) in flight; exactly two
        # writes ran: the in-flight one plus the coalesced latest. The
        # intermediate PHASE_SETUP_COMPLETE was dropped, not queued.
        assert eng.max_in_flight == 1
        assert len(eng.attempts) == 2
        assert eng.row["dispatch_phase"] == PHASE_STREAMING

    @pytest.mark.asyncio
    async def test_sequential_records_each_land(self) -> None:
        """Outside the in-flight window every durable phase lands, in order."""
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        for phase in (PHASE_LOADING_SETUP, PHASE_SETUP_COMPLETE, PHASE_STREAMING, PHASE_FIRST_NODE_DISPATCHED):
            writer.record(phase)
            await writer.flush()
            assert eng.row["dispatch_phase"] == phase
        assert eng.max_in_flight == 1

    @pytest.mark.asyncio
    async def test_flush_follows_a_writer_replaced_during_the_await(self) -> None:
        """If ``_task`` is replaced mid-flush, flush re-observes and drains again.

        Covers the loop-continuation arm: after awaiting the captured task a
        newly-scheduled drain must not be abandoned.
        """
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        loop = asyncio.get_running_loop()

        replacement = loop.create_future()
        replacement.set_result(None)

        async def _drain_replacing() -> None:
            # A fresh record replaced the in-flight drain while we were waiting.
            writer._task = replacement
            writer._pending = None

        writer._task = loop.create_task(_drain_replacing())
        await writer.flush()

        assert writer._task is replacement


class TestTrackerWiring:
    """The tracker records exactly DURABLE_PHASES when a writer is attached."""

    def test_durable_phases_are_the_wired_phases(self) -> None:
        # Pin the scope: the claim floor ('claimed') is never re-written here.
        # FAR-1422: first_node_dispatched is durable — a run that STARTED a node
        # must persist that fact, not look like it never dispatched anything.
        assert (
            frozenset({PHASE_LOADING_SETUP, PHASE_SETUP_COMPLETE, PHASE_STREAMING, PHASE_FIRST_NODE_DISPATCHED})
            == DURABLE_PHASES
        )

    @pytest.mark.asyncio
    async def test_tracker_records_each_durable_phase(self) -> None:
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        tracker = DispatchPhaseTracker(run_id=_RUN_ID, org_id=_ORG_ID)
        tracker.durable_writer = writer

        tracker.enter_phase(PHASE_LOADING_SETUP)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_LOADING_SETUP

        tracker.enter_phase(PHASE_SETUP_COMPLETE)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_SETUP_COMPLETE

        tracker.enter_phase(PHASE_STREAMING)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_STREAMING

        # FAR-1422: the first-node transition (executor on_first_progress site)
        # is durable and lands AFTER streaming — the monotonic guard accepts it
        # because it is entered later in wall-clock time.
        tracker.enter_phase(PHASE_FIRST_NODE_DISPATCHED)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_FIRST_NODE_DISPATCHED

    def test_non_durable_phases_are_not_written(self) -> None:
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        tracker = DispatchPhaseTracker(run_id=_RUN_ID, org_id=_ORG_ID)
        tracker.durable_writer = writer

        tracker.enter_phase(PHASE_CLAIMED)
        tracker.enter_phase(PHASE_GRAPH_COMPILE)
        # record() would synchronously create the drain task — none exists.
        assert writer._task is None
        assert not eng.attempts

    def test_tracker_without_writer_still_works(self) -> None:
        tracker = DispatchPhaseTracker(run_id=_RUN_ID, org_id=_ORG_ID)
        tracker.enter_phase(PHASE_STREAMING)
        assert tracker.phase == PHASE_STREAMING


class TestExecutorFirstNodeDispatchDurable:
    """FAR-1422: the executor's first-node event records the durable phase.

    The executor enters ``streaming`` before ``_stream_graph`` and calls
    ``enter_phase("first_node_dispatched")`` where ``on_first_progress``
    fires — the FIRST LangGraph event whose name is a node id. With a
    durable writer attached, that transition must land on
    ``runs.dispatch_phase`` so a run that STARTED a node and hung is
    distinguishable from one stuck in pre-node setup.
    """

    @pytest.mark.asyncio
    async def test_first_node_event_records_durable_phase(self) -> None:
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        tracker = DispatchPhaseTracker(run_id=_RUN_ID, org_id=_ORG_ID)
        tracker.durable_writer = writer

        # The executor enters streaming BEFORE _stream_graph (executor.py).
        tracker.enter_phase(PHASE_STREAMING)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_STREAMING

        executor = PipelineExecutor(MagicMock())
        executor._dispatch_phase_tracker = tracker
        progress: list[str] = []
        executor.on_first_progress = lambda: progress.append("first")

        run_uuid = uuid.uuid4()
        ctx = _StreamContext(
            node_ids={"node-a"},
            guard=None,
            completed_node_outputs=None,
            run_trace_id=None,
            broker=RunEventBroker(run_uuid),
            run_id=run_uuid,
            pipeline_id=None,
            org_id=uuid.UUID(_ORG_ID),
            node_token_budgets=None,
            eval_definitions_by_node=None,
            node_type_map=None,
        )
        state = _StreamState()

        result = await executor._handle_stream_event(
            state,
            ctx,
            {"event": "on_chain_start", "name": "node-a", "data": {}},
        )

        # Not a terminal event; first progress fired once; the tracker moved
        # to first_node_dispatched AND the durable row followed it.
        assert result is None
        assert progress == ["first"]
        assert tracker.phase == PHASE_FIRST_NODE_DISPATCHED
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_FIRST_NODE_DISPATCHED

    @pytest.mark.asyncio
    async def test_subsequent_node_event_does_not_re_enter_first_dispatch(self) -> None:
        """Only the FIRST node event signals first-progress (fires once)."""
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        tracker = DispatchPhaseTracker(run_id=_RUN_ID, org_id=_ORG_ID)
        tracker.durable_writer = writer

        executor = PipelineExecutor(MagicMock())
        executor._dispatch_phase_tracker = tracker
        progress: list[str] = []
        executor.on_first_progress = lambda: progress.append("first")

        run_uuid = uuid.uuid4()
        ctx = _StreamContext(
            node_ids={"node-a", "node-b"},
            guard=None,
            completed_node_outputs=None,
            run_trace_id=None,
            broker=RunEventBroker(run_uuid),
            run_id=run_uuid,
            pipeline_id=None,
            org_id=uuid.UUID(_ORG_ID),
            node_token_budgets=None,
            eval_definitions_by_node=None,
            node_type_map=None,
        )
        state = _StreamState()

        await executor._handle_stream_event(state, ctx, {"event": "on_chain_start", "name": "node-a", "data": {}})
        await executor._handle_stream_event(state, ctx, {"event": "on_chain_start", "name": "node-b", "data": {}})

        assert progress == ["first"]
        assert tracker.phase == PHASE_FIRST_NODE_DISPATCHED
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_FIRST_NODE_DISPATCHED
        # Exactly ONE write: only the first event entered the phase, so the
        # second node event scheduled no additional durable record.
        assert len(eng.attempts) == 1


class TestHistoryAppend:
    """FAR-1714 property 5a: every durable phase write appends a timeline entry.

    The append rides the SAME guarded UPDATE as the scalar phase columns, so
    it inherits the token fence and the monotonic guard — a rejected
    statement splices nothing.
    """

    @pytest.mark.asyncio
    async def test_each_durable_phase_appends_one_entry_in_order(self) -> None:
        eng = _FakeEngine(_fwd_row())
        writer = _writer(eng)
        for phase in (PHASE_LOADING_SETUP, PHASE_SETUP_COMPLETE, PHASE_STREAMING, PHASE_FIRST_NODE_DISPATCHED):
            writer.record(phase)
            await writer.flush()
        history = eng.row["dispatch_phase_history"]
        assert history is not None
        assert [entry["phase"] for entry in history] == [
            PHASE_LOADING_SETUP,
            PHASE_SETUP_COMPLETE,
            PHASE_STREAMING,
            PHASE_FIRST_NODE_DISPATCHED,
        ]
        # Every entry carries the timestamp the timeline is read by.
        assert all(entry["entered_at"] for entry in history)

    @pytest.mark.asyncio
    async def test_backwards_entry_does_not_append(self) -> None:
        # The monotonic guard rejects the statement — no phase move, NO splice.
        stored_future = datetime.now(UTC) + timedelta(seconds=30)
        eng = _FakeEngine(_row(stored_future, phase=PHASE_STREAMING))
        writer = _writer(eng)
        writer.record(PHASE_LOADING_SETUP)
        await writer.flush()
        assert eng.row["dispatch_phase"] == PHASE_STREAMING
        assert eng.row["dispatch_phase_history"] is None

    @pytest.mark.asyncio
    async def test_superseded_claim_token_does_not_append(self) -> None:
        # A successor re-claim rotated the token — the fence rejects, so the
        # replaced attempt cannot pollute the successor's timeline.
        eng = _FakeEngine(_row(datetime.now(UTC) - timedelta(seconds=1), token="tok-successor"))
        writer = _writer(eng)
        writer.record(PHASE_SETUP_COMPLETE)
        await writer.flush()
        assert eng.row["dispatch_phase"] == "claimed"
        assert eng.row["dispatch_phase_history"] is None

    def test_phase_update_appends_inside_the_guarded_statement(self) -> None:
        sql = str(_PHASE_UPDATE_SQL)
        assert "dispatch_phase_history=coalesce(dispatch_phase_history, '[]'::jsonb)" in sql
        assert "jsonb_build_array(jsonb_build_object('phase', :phase, 'entered_at', now()))" in sql
        # The append inherits BOTH guards — it is never a second, unfenced write.
        assert "claim_token=:tok" in sql
        assert "dispatch_phase_entered_at IS NULL OR dispatch_phase_entered_at <=" in sql


class TestClaimHistoryEntry:
    """FAR-1714 property 5b: all four claim statements append ``claimed``.

    The claim's history entry is the ATTEMPT BOUNDARY: the scalar floor only
    keeps the current attempt's claim time (later durable phases overwrite
    it), while history is cumulative — split the array on ``claimed`` entries
    to decompose claim→first-node per attempt.
    """

    def test_execute_claims_append_the_attempt_boundary(self) -> None:
        for stmt in (
            pe.build_claim_update(_stale_seconds=450),
            pe.build_claim_update(_stale_seconds=450, claim_token="tok-abc"),
        ):
            sql = str(stmt.compile(dialect=postgresql.dialect()))
            assert "dispatch_phase_history=coalesce(dispatch_phase_history, '[]'::jsonb)" in sql
            assert "jsonb_build_object('phase', 'claimed', 'entered_at', now())" in sql

    def test_resume_claims_append_the_attempt_boundary(self) -> None:
        for stmt in (
            pe.build_resume_claim_update(_stale_seconds=450),
            pe.build_resume_claim_update(_stale_seconds=450, claim_token="tok-abc"),
        ):
            sql = str(stmt.compile(dialect=postgresql.dialect()))
            assert "dispatch_phase_history=coalesce(dispatch_phase_history, '[]'::jsonb)" in sql
            assert "jsonb_build_object('phase', 'claimed', 'entered_at', now())" in sql

    def test_claim_still_stamps_the_scalar_floor_in_the_same_statement(self) -> None:
        # FAR-1088's contract must survive the history append.
        stmt = pe.build_claim_update(_stale_seconds=450, claim_token="tok-abc")
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "dispatch_phase='claimed'" in sql
        assert "dispatch_phase_entered_at=now()" in sql
        assert sql.count("UPDATE") == 1


class TestDropCounter:
    """FAR-1714 property 5c: abandoned writes are counted, never raised."""

    @pytest.mark.asyncio
    async def test_timeout_increments_the_counter_and_never_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        eng = _FakeEngine(_fwd_row())
        eng.block = asyncio.Event()  # never released: the phase write hangs
        writer = _writer(eng, timeout_seconds=0.05)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            writer.record(PHASE_STREAMING)
            await writer.flush()  # must not raise
        assert "dispatch_phase.write_timeout" in caplog.text
        assert eng.row["dispatch_phase"] == "claimed"
        # The abandoned write left a durable fingerprint.
        assert eng.row["dispatch_phase_write_drops"] == 1

    @pytest.mark.asyncio
    async def test_db_error_increments_the_counter_and_never_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        eng = _FakeEngine(_fwd_row())
        eng.raise_error = True
        writer = _writer(eng)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            writer.record(PHASE_LOADING_SETUP)
            await writer.flush()  # must not raise
        assert "dispatch_phase.write_failed" in caplog.text
        assert eng.row["dispatch_phase_write_drops"] == 1

    @pytest.mark.asyncio
    async def test_counter_write_failure_itself_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        """A bump that cannot land is logged and dropped — no recursion, no raise."""
        eng = _FakeEngine(_fwd_row())
        eng.raise_error = True  # the phase write fails...
        eng.raise_bump_error = True  # ...and so does its counter bump
        writer = _writer(eng)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            writer.record(PHASE_LOADING_SETUP)
            await writer.flush()  # must not raise
        assert "dispatch_phase.write_failed" in caplog.text
        assert "dispatch_phase.drop_counter_write_failed" in caplog.text
        assert eng.row["dispatch_phase_write_drops"] == 0

    @pytest.mark.asyncio
    async def test_bump_timeout_is_swallowed(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bump carries its own tighter bound, and failing it never raises."""
        assert DROP_COUNTER_WRITE_TIMEOUT_SECONDS < PHASE_WRITE_TIMEOUT_SECONDS

        eng = _FakeEngine(_fwd_row())
        eng.raise_error = True  # phase write fails -> drop recorded
        writer = _writer(eng)

        async def _hang_bump() -> None:
            await asyncio.Event().wait()

        writer._bump_drop_counter = _hang_bump  # type: ignore[method-assign]
        monkeypatch.setattr(pe, "DROP_COUNTER_WRITE_TIMEOUT_SECONDS", 0.01)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            writer.record(PHASE_STREAMING)
            await writer.flush()  # must not raise
        assert "dispatch_phase.drop_counter_timeout" in caplog.text

    def test_drop_counter_sql_is_additive_and_not_token_fenced(self) -> None:
        sql = str(_DROP_COUNTER_UPDATE_SQL)
        # Additive + run-scoped: a superseded attempt's drops still land.
        assert "dispatch_phase_write_drops=dispatch_phase_write_drops+1" in sql
        assert "claim_token" not in sql
        assert "id=:rid" in sql
        assert "organisation_id=:oid" in sql


class TestExecuteRunWiresWriter:
    """saq_worker.execute_run constructs the writer bound to the claim."""

    @pytest.mark.asyncio
    async def test_execute_run_attaches_writer_bound_to_claim(self) -> None:
        job = MagicMock()
        job.update = AsyncMock()
        ctx: dict[str, Any] = {"job": job}

        async def _pass_through(aeng: Any, **kwargs: Any) -> dict[str, str]:
            await kwargs["execute_fn"]()
            return {"status": "complete"}

        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=_TOK),
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock) as load,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock),
            patch(
                "modulo.core.pipeline_execution.run_executor_with_watchdog",
                side_effect=_pass_through,
            ) as watchdog,
        ):
            run = MagicMock()
            run.input_payload = {"a": 1}
            executor = MagicMock()
            executor.execute = AsyncMock()
            load.return_value = (run, executor)
            result = await sw.execute_run(ctx, run_id=_RUN_ID, org_id=_ORG_ID)

        assert result == {"status": "complete"}
        tracker = watchdog.await_args.kwargs["dispatch_tracker"]
        writer = tracker.durable_writer
        assert writer is not None
        assert isinstance(writer, DispatchPhaseWriter)
        assert writer.claim_token == _TOK
        assert writer.run_id == _RUN_ID
        assert writer.org_id == _ORG_ID

    @pytest.mark.asyncio
    async def test_execute_run_skips_flush_when_no_writer_attached(self) -> None:
        """The post-run flush is guarded: a tracker with no writer is skipped."""
        job = MagicMock()
        job.update = AsyncMock()
        ctx: dict[str, Any] = {"job": job}

        async def _clear_writer(aeng: Any, **kwargs: Any) -> dict[str, str]:
            kwargs["dispatch_tracker"].durable_writer = None
            return {"status": "failed"}

        with (
            patch.object(sw, "_get_async_engine", return_value=MagicMock()),
            patch("modulo.core.pipeline_execution.claim_run_async", new_callable=AsyncMock, return_value=_TOK),
            patch("modulo.core.pipeline_execution.load_and_setup", new_callable=AsyncMock) as load,
            patch("modulo.core.pipeline_execution.mark_complete", new_callable=AsyncMock) as complete,
            patch(
                "modulo.core.pipeline_execution.run_executor_with_watchdog",
                side_effect=_clear_writer,
            ),
        ):
            run = MagicMock()
            run.input_payload = {}
            executor = MagicMock()
            executor.execute = AsyncMock()
            load.return_value = (run, executor)
            result = await sw.execute_run(ctx, run_id=_RUN_ID, org_id=_ORG_ID)

        assert result == {"status": "failed"}
        complete.assert_not_awaited()
