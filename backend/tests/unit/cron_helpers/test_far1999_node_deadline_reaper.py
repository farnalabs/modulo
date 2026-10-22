"""FAR-1999 — bound how long a node-started run holds a concurrency slot.

Ticket measurement (48h, app.modulo.run trigger-event log): 70 runs averaged
135 minutes terminalised ``run.no_progress`` by the mid-graph age gate with NO
node envelope at all. Those runs had STARTED a node (``dispatch_phase =
'first_node_dispatched'``) but produced no checkpoint, no output and no token
usage, so they were excluded from the zero-progress shape (which covers
never-started runs) and only the ~135-minute age gate could collect them — the
whole time holding one of the trigger's eight concurrency slots.

This module pins the fix: a node-started run with ZERO node progress whose
durable node deadline (first-node start + the graph's max node
``timeout_seconds``) has provably passed, and whose executor heartbeat is
stale, is terminal-failed by dispatcher_reconcile at that deadline instead of
two hours later.

Covered:
  * the zero-progress / node-started decision helpers;
  * the durable deadline derivation (phase anchor, attempt-clock fallback,
    graph maximum, ``None`` when no anchor);
  * the chokepoint's guards (only a running, node-started, zero-progress,
    stale-heartbeat row past its deadline is reaped; a node that reported in
    the SELECT→write window is never killed);
  * the row loop wiring (a due row is reaped and never re-dispatched; a
    not-yet-due row keeps the FAR-1803 hold-and-report path).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Column, MetaData, Table, Text, Uuid, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine.workspace_input_audit import AUDIT_NODE_ID
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, RunNodeOutput

ORG = uuid.uuid4()
STALE_WINDOW_SECONDS = 600
NODE_BUDGET_SECONDS = 3300


def _node_started_run(
    *,
    phase_entered_at: datetime | None,
    started_at: datetime | None = None,
    node_token_usage: Any = None,
    error_code: str | None = None,
) -> SimpleNamespace:
    """A running, node-started, stale-heartbeat run row (reaper input)."""
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        trigger_id=uuid.uuid4(),
        status="running",
        dispatcher="saq",
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
        dispatch_phase_entered_at=phase_entered_at,
        dispatched_at=(started_at or now) - timedelta(minutes=5),
        heartbeat_at=now - timedelta(minutes=30),
        started_at=started_at or now - timedelta(minutes=70),
        node_token_usage=node_token_usage,
        outputs_absent=True,
        checkpoints_absent=True,
        claim_count=1,
        error_code=error_code,
        error_detail=None,
        completed_at=None,
        node_deadline_watchdog_fired_count=0,
        guardrail_override_resume_pending=False,
        sandbox_dispatch_state=None,
        max_concurrent_runs=5,
    )


class _ReaperSession:
    """Session double for :func:`ch._fail_node_deadline_run`."""

    def __init__(
        self,
        run: Any,
        *,
        outputs_absent: bool = True,
        checkpoints_absent: bool = True,
        return_run: bool = True,
    ) -> None:
        self._run = run
        self._return_run = return_run
        self._flags = SimpleNamespace(
            outputs_absent=outputs_absent,
            checkpoints_absent=checkpoints_absent,
        )
        self.flushed = False
        self.executed: list[Any] = []

    async def get(self, model: Any, pk: Any, with_for_update: bool = False) -> Any:
        return self._run if self._return_run else None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append(stmt)
        result = MagicMock()
        result.one.return_value = self._flags
        return result

    async def flush(self) -> None:
        self.flushed = True


class _NoFlagsRow:
    """A row source without the reconcile flags (fail-closed probe)."""

    status = "running"
    dispatcher = "saq"
    dispatch_phase = PHASE_FIRST_NODE_DISPATCHED
    node_token_usage = None
    started_at = datetime.now(UTC) - timedelta(minutes=70)


class TestZeroProgressEvidence:
    def test_evidence_requires_all_three_legs(self) -> None:
        row = SimpleNamespace(node_token_usage=None, outputs_absent=True, checkpoints_absent=True)
        assert ch._has_zero_progress_evidence(row) is True
        row.node_token_usage = {"n": 1}
        assert ch._has_zero_progress_evidence(row) is False

    def test_missing_flags_fail_closed(self) -> None:
        assert ch._has_zero_progress_evidence(_NoFlagsRow()) is False

    def test_node_started_no_progress_requires_the_phase(self) -> None:
        row = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=70))
        assert ch._is_node_started_no_progress_row(row) is True

    def test_null_phase_is_not_the_node_started_class(self) -> None:
        row = _node_started_run(phase_entered_at=None)
        row.dispatch_phase = None
        assert ch._is_node_started_no_progress_row(row) is False

    def test_zero_progress_shape_excludes_node_started(self) -> None:
        # The existing zero-progress shape is the NEVER-started class; the
        # node-started class is its own (FAR-1999) predicate.
        row = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=70))
        assert ch._is_zero_progress_shape_row(row) is False
        assert ch._is_node_started_no_progress_row(row) is True


class TestNodeStartedDeadline:
    def test_phase_anchor_wins_over_the_attempt_clock(self) -> None:
        phase_entered = datetime.now(UTC) - timedelta(minutes=90)
        row = _node_started_run(phase_entered_at=phase_entered)
        deadline = ch._node_started_deadline(row, node_budget_seconds=NODE_BUDGET_SECONDS)
        assert deadline == phase_entered + timedelta(seconds=NODE_BUDGET_SECONDS)

    def test_falls_back_to_the_attempt_clock_plus_setup_grace(self) -> None:
        started = datetime.now(UTC) - timedelta(minutes=90)
        row = _node_started_run(phase_entered_at=None, started_at=started)
        with patch.object(ch, "get_settings", return_value=MagicMock(saq_setup_grace_seconds=600)):
            deadline = ch._node_started_deadline(row, node_budget_seconds=NODE_BUDGET_SECONDS)
        # ``started_at`` is the later of the two attempt anchors.
        assert deadline == started + timedelta(seconds=600 + NODE_BUDGET_SECONDS)

    def test_no_time_anchor_yields_no_deadline(self) -> None:
        row = _node_started_run(phase_entered_at=None)
        row.dispatched_at = None
        row.started_at = None
        assert ch._node_started_deadline(row, node_budget_seconds=NODE_BUDGET_SECONDS) is None

    def test_row_budget_reads_the_graph_maximum(self) -> None:
        row = SimpleNamespace(
            snapshot_graph_json={"nodes": [{"timeout_seconds": 120}, {"timeout_seconds": 3300}]},
            pipeline_graph_json=None,
            pipeline_node_timeout_seconds=None,
        )
        assert ch._row_node_budget_seconds(row) == NODE_BUDGET_SECONDS

    def test_row_budget_never_under_estimates_the_watchdog_default(self) -> None:
        # The in-process watchdog uses SAQ_NODE_DEFAULT_TIMEOUT_SECONDS for a
        # node with no explicit timeout; the pipeline-level default is NOT that
        # fallback, so a small pipeline default must not shorten the reaper's
        # budget below the watchdog's.
        row = SimpleNamespace(
            snapshot_graph_json={"nodes": [{"id": "a"}]},
            pipeline_graph_json=None,
            pipeline_node_timeout_seconds=300,
        )
        with patch.object(ch, "get_settings", return_value=MagicMock(saq_node_default_timeout_seconds=1200)):
            assert ch._row_node_budget_seconds(row) == 1200

    def test_row_budget_falls_back_to_the_settings_default(self) -> None:
        row = SimpleNamespace(
            snapshot_graph_json=None,
            pipeline_graph_json=None,
            pipeline_node_timeout_seconds=None,
        )
        with patch.object(ch, "get_settings", return_value=MagicMock(saq_node_default_timeout_seconds=1200)):
            assert ch._row_node_budget_seconds(row) == 1200

    def test_settings_double_without_the_field_uses_the_coded_default(self) -> None:
        # A MagicMock settings double exposes non-int attributes; the helper
        # must fall back rather than raise (the nodeless-floor convention).
        row = SimpleNamespace(
            snapshot_graph_json=None,
            pipeline_graph_json=None,
            pipeline_node_timeout_seconds=None,
        )
        with patch.object(ch, "get_settings", return_value=MagicMock()):
            assert ch._row_node_budget_seconds(row) == 1200


class TestFailNodeDeadlineRun:
    @pytest.mark.asyncio
    async def test_reaps_a_due_node_started_no_progress_run(self) -> None:
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        session = _ReaperSession(run)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is True
        assert run.status == "failed"
        assert run.error_code == ch._NODE_DEADLINE_EXCEEDED_ERROR_CODE
        assert run.node_deadline_watchdog_fired_count == 1
        assert run.completed_at is not None
        assert summary["node_deadline_reaped"] == 1
        assert session.flushed is True
        # Terminal-fail only: the run's retry policy is never consulted and no
        # re-dispatch job is produced.
        assert run.error_detail is not None

    @pytest.mark.asyncio
    async def test_does_not_reap_before_the_deadline(self) -> None:
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=5))
        session = _ReaperSession(run)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert run.status == "running"
        assert run.error_code is None
        assert summary["node_deadline_reaped"] == 0

    @pytest.mark.asyncio
    async def test_does_not_reap_when_a_node_reported_after_the_scan(self) -> None:
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        session = _ReaperSession(run, checkpoints_absent=False)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert run.status == "running"
        assert summary["node_deadline_reaped"] == 0

    @pytest.mark.asyncio
    async def test_does_not_reap_when_token_usage_landed_after_the_scan(self) -> None:
        """Zero-progress race: the node finalised between the scan and the lock.

        ``node_token_usage`` is written when a run finalises, so a non-NULL
        value on the locked row proves the node reported after the scan
        snapshot. The locked row's token usage is re-checked BEFORE the
        reconcile-flag query, so a run that made progress is never
        terminal-failed as zero-progress (and the guard never even reaches the
        extra query). Removing the guard reaps this row and fails the test.
        """
        run = _node_started_run(
            phase_entered_at=datetime.now(UTC) - timedelta(minutes=90),
            node_token_usage={"prompt_tokens": 10},
        )
        session = _ReaperSession(run)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert run.status == "running"
        assert summary["node_deadline_reaped"] == 0
        # The guard short-circuits before the reconcile-flag query and the write.
        assert not session.executed
        assert session.flushed is False

    @pytest.mark.asyncio
    async def test_does_not_reap_when_the_locked_row_did_not_start_a_node(self) -> None:
        """Defence in depth: the locked row's phase is re-checked on its own.

        ``_is_node_started_executor_lost_row`` already requires
        ``first_node_dispatched``, so for every row the classifier accepts the
        two checks agree. The reaper STILL re-checks the phase on the row it
        locked with ``with_for_update`` instead of trusting the classification
        alone: a classifier change (or a wider future predicate) must never let
        this terminal-fail a run that never started a node. Force the
        classifier to accept and prove the locked-row recheck still refuses.
        Removing the recheck reaps this row and fails the test.
        """
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        run.dispatch_phase = None
        session = _ReaperSession(run)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with (
            patch.object(ch, "_is_node_started_executor_lost_row", return_value=True),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
        ):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert run.status == "running"
        assert summary["node_deadline_reaped"] == 0
        assert not session.executed
        assert session.flushed is False

    @pytest.mark.asyncio
    async def test_does_not_reap_a_missing_row(self) -> None:
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        session = _ReaperSession(run, return_run=False)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert summary["node_deadline_reaped"] == 0

    @pytest.mark.asyncio
    async def test_does_not_reap_a_fresh_heartbeat_row(self) -> None:
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        run.heartbeat_at = datetime.now(UTC)
        session = _ReaperSession(run)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert run.status == "running"
        assert summary["node_deadline_reaped"] == 0


class TestRowLoopReaperWiring:
    """The ``_reconcile_one_row`` branch: reap when due, otherwise hold."""

    @pytest.mark.asyncio
    async def test_due_row_is_reaped_and_never_redispatched(self) -> None:
        row = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        row.snapshot_graph_json = {"nodes": [{"timeout_seconds": NODE_BUDGET_SECONDS}]}
        summary: dict[str, Any] = {"node_started_executor_lost": 0}
        terminalized: list[Any] = []
        reaper = AsyncMock(return_value=True)
        with patch.object(ch, "_fail_node_deadline_run", reaper):
            got = await ch._reconcile_one_row(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                ORG,
                row,
                35,
                0,
                summary,
                terminalized,
                stale_window=STALE_WINDOW_SECONDS,
            )

        assert got == 0
        reaper.assert_awaited_once()
        # The held counter must NOT fire when the row was reaped.
        assert summary["node_started_executor_lost"] == 0
        assert terminalized == [(row.id, ORG)]

    @pytest.mark.asyncio
    async def test_not_yet_due_row_keeps_the_hold_and_report_path(self) -> None:
        row = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=5))
        row.snapshot_graph_json = {"nodes": [{"timeout_seconds": NODE_BUDGET_SECONDS}]}
        summary: dict[str, Any] = {"node_started_executor_lost": 0}
        terminalized: list[Any] = []
        reaper = AsyncMock(return_value=True)
        with patch.object(ch, "_fail_node_deadline_run", reaper):
            got = await ch._reconcile_one_row(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                ORG,
                row,
                35,
                0,
                summary,
                terminalized,
                stale_window=STALE_WINDOW_SECONDS,
            )

        assert got == 0
        reaper.assert_not_awaited()
        assert summary["node_started_executor_lost"] == 1
        assert not terminalized

    @pytest.mark.asyncio
    async def test_progressing_node_started_row_is_not_reaped(self) -> None:
        row = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        row.snapshot_graph_json = {"nodes": [{"timeout_seconds": NODE_BUDGET_SECONDS}]}
        row.checkpoints_absent = False
        summary: dict[str, Any] = {"node_started_executor_lost": 0}
        terminalized: list[Any] = []
        reaper = AsyncMock(return_value=True)
        with patch.object(ch, "_fail_node_deadline_run", reaper):
            got = await ch._reconcile_one_row(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                ORG,
                row,
                35,
                0,
                summary,
                terminalized,
                stale_window=STALE_WINDOW_SECONDS,
            )

        assert got == 0
        reaper.assert_not_awaited()
        assert summary["node_started_executor_lost"] == 1
        assert not terminalized


# ---------------------------------------------------------------------------
# FAR-2052: a bookkeeping ``__final__`` row is NOT node progress
# ---------------------------------------------------------------------------
# The zero-progress legs mean "no NODE finalised a store row". The managed
# workspace-input audit (FAR-801) writes ``(run_id, '__mwi_audit__',
# '__final__')`` BEFORE any node runs, so an unscoped ``__final__`` leg made a
# run whose inputs were merely resolved read as already-progressing: the
# nodeless repairs and this reaper skipped a run that never completed a node.
# These tests execute the REAL predicates against REAL seeded rows, so the
# verdict is observed rather than injected as a hand-set flag.


def _run_row(*, org_id: uuid.UUID, run_number: int, thread_id: str) -> dict[str, Any]:
    """One insertable ``runs`` row carrying the NOT-NULL columns only."""
    return {
        "id": uuid.uuid4(),
        "organisation_id": org_id,
        "pipeline_id": uuid.uuid4(),
        "snapshot_id": uuid.uuid4(),
        "trigger_type": "manual",
        "run_number": run_number,
        "input_hash": f"{run_number:064d}",
        "langgraph_thread_id": thread_id,
        "status": "running",
        "started_at": datetime.now(UTC),
    }


def _final_row(
    org_id: uuid.UUID, run_row: dict[str, Any], node_id: str, outputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One insertable ``__final__`` row for *run_row* and *node_id*.

    *outputs* is the row's ``outputs_json`` payload — real audit rows and the
    run-metadata row always carry one (the metadata row's is CHECK-enforced).
    """
    return {
        "organisation_id": org_id,
        "run_id": run_row["id"],
        "node_id": node_id,
        "attempt_key": FINAL_ATTEMPT_KEY,
        "outputs_json": outputs,
    }


@pytest.fixture
async def final_row_db() -> AsyncGenerator[dict[str, Any], None]:
    """Three runs against a real (in-memory SQLite) ``run_node_outputs`` schema.

    * ``audit_only`` — an otherwise zero-progress run whose ONLY ``__final__``
      row is the pre-node workspace-input audit (the FAR-2052 defect case).
    * ``real_node`` — the same audit row PLUS a genuine completed node's row.
    * ``meta_only`` — only the run-metadata bookkeeping row.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    checkpoints = Table(
        "checkpoints",
        MetaData(),
        Column("organisation_id", Uuid),
        Column("thread_id", Text),
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__, RunNodeOutput.__table__])
        )
        await conn.run_sync(lambda sync_conn: checkpoints.create(sync_conn, checkfirst=True))
    org_id = uuid.uuid4()
    runs = {
        "audit_only": _run_row(org_id=org_id, run_number=1, thread_id="thread-audit"),
        "real_node": _run_row(org_id=org_id, run_number=2, thread_id="thread-real"),
        "meta_only": _run_row(org_id=org_id, run_number=3, thread_id="thread-meta"),
    }
    async with engine.begin() as conn:
        await conn.execute(Run.__table__.insert(), list(runs.values()))
        await conn.execute(
            RunNodeOutput.__table__.insert(),
            [
                # FAR-801's pre-node audit row: the ONLY __final__ row of
                # ``audit_only`` (it carries the resolved-input payload, exactly
                # like the production write).
                _final_row(org_id, runs["audit_only"], AUDIT_NODE_ID, {"workspace_inputs": []}),
                # The same audit row on a run that also completed a node.
                _final_row(org_id, runs["real_node"], AUDIT_NODE_ID, {"workspace_inputs": []}),
                _final_row(org_id, runs["real_node"], "node-1"),
                _final_row(
                    org_id,
                    runs["meta_only"],
                    META_NODE_ID,
                    {"empty_outputs": False, "empty_telemetry": False},
                ),
            ],
        )
    try:
        yield {"engine": engine, "runs": runs}
    finally:
        await engine.dispose()


async def _outputs_absent(engine: Any, run_id: uuid.UUID) -> bool:
    """The reconcile scan's ``outputs_absent`` flag for one run, from real rows."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(Run.id, ch._final_row_absent_expr().label("outputs_absent")).where(Run.id == run_id)
            )
        ).one()
    return bool(row.outputs_absent)


class TestBookkeepingFinalRowIsNotProgress:
    """FAR-2052 — only a real node's ``__final__`` row disproves zero progress."""

    async def test_audit_only_run_reports_no_node_final_row(self, final_row_db: dict[str, Any]) -> None:
        flags = await _outputs_absent(final_row_db["engine"], final_row_db["runs"]["audit_only"]["id"])
        assert flags is True

    async def test_run_metadata_row_alone_is_not_node_progress(self, final_row_db: dict[str, Any]) -> None:
        flags = await _outputs_absent(final_row_db["engine"], final_row_db["runs"]["meta_only"]["id"])
        assert flags is True

    async def test_a_real_node_final_row_is_not_zero_progress(self, final_row_db: dict[str, Any]) -> None:
        flags = await _outputs_absent(final_row_db["engine"], final_row_db["runs"]["real_node"]["id"])
        assert flags is False

    async def test_reaper_classifier_selects_the_audit_only_run(self, final_row_db: dict[str, Any]) -> None:
        row = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        row.outputs_absent = await _outputs_absent(final_row_db["engine"], final_row_db["runs"]["audit_only"]["id"])
        assert ch._is_node_started_no_progress_row(row) is True

    async def test_reaper_path_terminalises_the_audit_only_run(self, final_row_db: dict[str, Any]) -> None:
        """The reaper's own chokepoint, fed the DB-derived flag."""
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        flags = await _outputs_absent(final_row_db["engine"], final_row_db["runs"]["audit_only"]["id"])
        session = _ReaperSession(run, outputs_absent=flags)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is True
        assert run.status == "failed"
        assert run.error_code == ch._NODE_DEADLINE_EXCEEDED_ERROR_CODE

    async def test_reaper_path_leaves_the_real_node_run_alone(self, final_row_db: dict[str, Any]) -> None:
        run = _node_started_run(phase_entered_at=datetime.now(UTC) - timedelta(minutes=90))
        flags = await _outputs_absent(final_row_db["engine"], final_row_db["runs"]["real_node"]["id"])
        session = _ReaperSession(run, outputs_absent=flags)
        summary: dict[str, Any] = {"node_deadline_reaped": 0}
        with patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock):
            reaped = await ch._fail_node_deadline_run(
                session,
                run.id,
                ORG,
                summary,
                stale_window=STALE_WINDOW_SECONDS,
                node_budget_seconds=NODE_BUDGET_SECONDS,
            )

        assert reaped is False
        assert run.status == "running"
        assert summary["node_deadline_reaped"] == 0

    async def test_raw_zero_progress_shape_selects_only_bookkeeping_only_runs(
        self, final_row_db: dict[str, Any]
    ) -> None:
        """The raw-SQL twin (nodeless router / age gate / SAQ sweep) agrees."""
        async with final_row_db["engine"].connect() as conn:
            selected = {
                uuid.UUID(str(value))
                for (value,) in (
                    await conn.execute(
                        # Static template + str.replace (never f-string/``+``) keeps
                        # the statement text identical to the one the router, age
                        # gate and SAQ sweep assemble.
                        text("SELECT id FROM runs WHERE __SHAPE__".replace("__SHAPE__", ch._ZERO_PROGRESS_SHAPE_SQL)),
                        {
                            "final_key": FINAL_ATTEMPT_KEY,
                            "first_node_phase": PHASE_FIRST_NODE_DISPATCHED,
                        },
                    )
                ).all()
            }

        runs = final_row_db["runs"]
        assert runs["audit_only"]["id"] in selected
        assert runs["meta_only"]["id"] in selected
        assert runs["real_node"]["id"] not in selected
