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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED

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
