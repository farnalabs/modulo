"""FAR-1088 (W6) — three confirmations the dispatch-phase slice must hold.

1. Done-means #4: a healthy, progressing run is NEVER selected by the
   claimed-but-nodeless zombie machinery — the row-level predicate rejects it,
   the SQL scan keeps its progress legs, and the stale-heartbeat repair branch
   only admits a running run once its heartbeat is stale.
2. The instrumentation columns stay internal: ``dispatch_phase`` /
   ``dispatch_phase_entered_at`` are not projected by the run API
   (``RunResponse`` schema, detail payload, or list item).
3. Every claim re-stamps ``dispatch_phase='claimed'`` (+ entry time) in the
   SAME UPDATE that claims the row — all FOUR claim sites: the execute claim
   variants AND the HITL resume claim variants — so a re-claim RESETS the
   phase and a re-dispatched or resumed run can never report a previous
   attempt's phase.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from modulo.api.routes.runs import (
    RunResponse,
    _build_list_item,
    _build_run_response,
    _ListPageContext,
)
from modulo.core import cron_helpers as ch
from modulo.core import pipeline_execution as pe
from modulo.db.models.run import Run

_RUN_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()


def _progressing_row(
    *,
    node_token_usage: dict[str, Any] | None,
    outputs_absent: bool,
    checkpoints_absent: bool,
    started_minutes_ago: float,
    heartbeat_minutes_ago: float,
) -> SimpleNamespace:
    """A running SAQ row in the shape the reconcile scan hands to the
    row-level re-check (``outputs_absent`` / ``checkpoints_absent`` are the
    scan's NOT EXISTS flags)."""
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        status="running",
        dispatcher="saq",
        node_token_usage=node_token_usage,
        outputs_absent=outputs_absent,
        checkpoints_absent=checkpoints_absent,
        started_at=now - timedelta(minutes=started_minutes_ago),
        heartbeat_at=now - timedelta(minutes=heartbeat_minutes_ago),
        dispatched_at=now - timedelta(minutes=5),
        claim_count=1,
        retry_policy=None,
    )


# ---------------------------------------------------------------------------
# 1. A healthy, progressing run is never failed/repaired by this machinery
#    (ticket Done-means #4).
# ---------------------------------------------------------------------------


class TestHealthyRunNeverSelectedByNodelessMachinery:
    def test_run_with_dispatched_nodes_is_not_a_nodeless_zombie(self) -> None:
        """A run that HAS executed nodes (finalised token usage, a
        ``__final__`` store row) is not a claimed-but-nodeless zombie, however
        old it is — the row-level re-check rejects it before the repair branch
        can fail it."""
        row = _progressing_row(
            node_token_usage={},
            outputs_absent=False,
            checkpoints_absent=False,
            started_minutes_ago=60,
            heartbeat_minutes_ago=30,
        )

        assert ch._is_nodeless_zombie_row(row, 35) is False

    async def test_progressing_run_falls_through_the_nodeless_branch_untouched(self) -> None:
        """The nodeless repair returns ``None`` (row continues down the normal
        path) for a progressing run: no terminal-fail, no skip, no repair."""
        row = _progressing_row(
            node_token_usage={},
            outputs_absent=False,
            checkpoints_absent=False,
            started_minutes_ago=60,
            heartbeat_minutes_ago=30,
        )
        summary = {"nodeless_failed": 0, "nodeless_redispatched": 0, "nodeless_capped": 0, "skipped": 0}

        handled = await ch._reconcile_nodeless_repair(
            AsyncMock(),
            MagicMock(),
            uuid.uuid4(),
            row,
            35,
            0,
            summary,
            [],
        )

        assert handled is None
        assert summary["nodeless_failed"] == 0
        assert summary["skipped"] == 0

    def test_scan_predicate_keeps_its_progress_guards(self) -> None:
        """The SQL scan (the gate that decides whether a row ever reaches the
        row-level re-check) gates on run finalisation and super-step progress:
        finalised token usage, a ``__final__`` store row, and a LangGraph
        checkpoint all keep a progressing run out of the zombie match."""
        sql = str(sa.select(Run.id).where(ch._nodeless_zombie_predicate(35)).compile())

        assert "runs.node_token_usage IS NULL" in sql
        assert "NOT (EXISTS" in sql
        assert "FROM checkpoints" in sql
        assert "FROM run_node_outputs" in sql

    def test_recent_heartbeat_running_run_excluded_from_the_repair_scan(self) -> None:
        """The running+SAQ repair branch only admits a run whose heartbeat is
        STALE past ``stale_window`` — a run with a recent heartbeat fails the
        clause, so the reconcile scan never selects it and never repairs it."""
        sql = str(
            ch._build_re_dispatch_predicate(
                reenqueue_window=600,
                stale_window=300,
                capacity_redispatch_seconds=120,
            ).compile(compile_kwargs={"literal_binds": True})
        )

        assert (
            "runs.status = 'running' AND runs.dispatcher = 'saq' "
            "AND runs.heartbeat_at < now() - 300 * interval '1 second'"
        ) in sql


# ---------------------------------------------------------------------------
# 2. The two new columns are not API-projected.
# ---------------------------------------------------------------------------


def _make_run(**overrides: object) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.status = "complete"
    run.pipeline_id = _PIPELINE_ID
    run.pipeline = None
    run.run_number = 1
    run.langgraph_thread_id = "thread-1"
    run.snapshot_id = _SNAPSHOT_ID
    run.error_detail = None
    run.error_code = None
    run.total_cost_usd = None
    run.total_tokens = None
    run.node_token_usage = None
    run.cost_breakdown = None
    run.trigger_type = None
    run.trigger_id = None
    run.account_id = None
    run.heartbeat_at = None
    run.work_item_refs = None
    run.input_payload = None
    run.created_at = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    run.started_at = datetime(2026, 8, 1, 10, 1, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 8, 1, 10, 5, 30, tzinfo=UTC)
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


class TestDispatchPhaseNotApiProjected:
    def test_columns_absent_from_run_response_schema(self) -> None:
        """``dispatch_phase`` / ``dispatch_phase_entered_at`` are internal
        instrumentation: neither may appear in the run detail response model
        (fields or generated JSON schema)."""
        assert "dispatch_phase" not in RunResponse.model_fields
        assert "dispatch_phase_entered_at" not in RunResponse.model_fields

        properties = RunResponse.model_json_schema()["properties"]
        assert "dispatch_phase" not in properties
        assert "dispatch_phase_entered_at" not in properties

    def test_columns_absent_from_detail_payload(self) -> None:
        """The payload ``_build_run_response`` actually serialises carries
        neither column."""
        dumped = _build_run_response(_make_run()).model_dump()

        assert "dispatch_phase" not in dumped
        assert "dispatch_phase_entered_at" not in dumped

    def test_columns_absent_from_list_item_payload(self) -> None:
        """The list endpoint's per-row builder emits neither column."""
        ctx = _ListPageContext(
            child_rollup={},
            account_labels={},
            trigger_labels={},
            active_count=0,
            concurrency_limit=None,
        )

        item = _build_list_item(_make_run(), ctx)

        assert "dispatch_phase" not in item
        assert "dispatch_phase_entered_at" not in item


# ---------------------------------------------------------------------------
# 3. A re-claim resets the phase.
# ---------------------------------------------------------------------------


class TestClaimResetsDispatchPhase:
    """ALL FOUR claim UPDATE variants — the two execute variants and the two
    HITL resume variants — stamp ``dispatch_phase='claimed'`` (and its entry
    time) in the SAME statement that claims the row, so every re-claim
    (including a re-dispatch's claim and a resume claim) resets the phase, and
    a re-woken run can never report a previous attempt's phase."""

    def test_plain_claim_stamps_the_phase(self) -> None:
        stmt = pe.build_claim_update(_stale_seconds=450)
        sql = str(stmt.compile(dialect=postgresql.dialect()))

        assert "dispatch_phase='claimed'" in sql
        assert "dispatch_phase_entered_at=now()" in sql
        # The stamp rides the claim's own counter increment: every re-claim
        # re-stamps, which is what makes the reset unconditional.
        assert "claim_count=claim_count+1" in sql

    def test_token_claim_stamps_the_phase(self) -> None:
        stmt = pe.build_claim_update(_stale_seconds=450, claim_token="tok-abc")
        sql = str(stmt.compile(dialect=postgresql.dialect()))

        assert "dispatch_phase='claimed'" in sql
        assert "dispatch_phase_entered_at=now()" in sql

    def test_resume_plain_claim_stamps_the_phase(self) -> None:
        """The resume claim (saq_worker.resume_run → claim_resume_run_async)
        stamps the SAME floor as the execute claim — before FAR-1088 W-slice
        follow-up it did not, leaving a resumed run's phase floor stale."""
        stmt = pe.build_resume_claim_update(_stale_seconds=450)
        sql = str(stmt.compile(dialect=postgresql.dialect()))

        assert "dispatch_phase='claimed'" in sql
        assert "dispatch_phase_entered_at=now()" in sql
        assert "claim_count=claim_count+1" in sql

    def test_resume_token_claim_stamps_the_phase(self) -> None:
        """Both resume claim sites stamp: with and without a claim token."""
        stmt = pe.build_resume_claim_update(_stale_seconds=450, claim_token="tok-abc")
        sql = str(stmt.compile(dialect=postgresql.dialect()))

        assert "dispatch_phase='claimed'" in sql
        assert "dispatch_phase_entered_at=now()" in sql
        assert "claim_count=claim_count+1" in sql
        # The raw (uncompiled) template carries the named bind — the compiled
        # postgres dialect renders it as a pyformat param.
        assert "claim_token=:tok" in str(stmt)


# ---------------------------------------------------------------------------
# W-B: a run with a LIVE sandbox_dispatch_state marker is NOT a zero-node
# nodeless zombie — the backstop leaves it to the node-deadline watchdog until
# the safety floor passes, then catches it again.
# ---------------------------------------------------------------------------


class TestInFlightDispatchExcludedFromNodelessBackstop:
    """FAR-1088 W-B: the nodeless predicate and row recheck carry an in-flight leg.

    A live dispatch marker is written when a node dispatches and cleared fenced
    when it completes, so a row carrying one has dispatched a node: claiming it
    at the 35-minute window boundary (the Branch Fixer dispatcher_reconcile
    kill) superseded a legitimately in-flight executor. The leg excludes such
    rows until the safety floor (``_NODELESS_IN_FLIGHT_FLOOR_SECONDS`` — the
    worst-case node deadline from ``started_at``), so nothing can hang forever
    if the FAR-369 node-deadline watchdog itself has failed.
    """

    @staticmethod
    def _inflight_row(*, started_minutes_ago: float) -> SimpleNamespace:
        """A zero-node row (no token usage, no ``__final__`` row, no
        checkpoints) carrying a LIVE dispatch marker — dispatched, no
        super-step completed yet."""
        row = _progressing_row(
            node_token_usage=None,
            outputs_absent=True,
            checkpoints_absent=True,
            started_minutes_ago=started_minutes_ago,
            heartbeat_minutes_ago=0.5,  # executor alive — only the nodeless branch matches
        )
        row.sandbox_dispatch_state = json.dumps(
            {"state": "dispatching", "attempt_key": "att-1", "provider": "e2b"},
        )
        return row

    def test_live_marker_row_is_not_a_nodeless_zombie(self) -> None:
        """A live marker row past the 35-min nodeless window is NOT a
        zero-node zombie (Fails without W-B: it returned True)."""
        row = self._inflight_row(started_minutes_ago=40)

        assert ch._is_nodeless_zombie_row(row, 35) is False

    async def test_live_marker_row_falls_through_the_nodeless_repair(self) -> None:
        """``_reconcile_nodeless_repair`` returns ``None`` (row continues down
        the normal path) for an in-flight row: no terminal-fail, no skip, no
        re-dispatch. Fails without W-B: the row check passed and the branch
        handled the row (throttled skip -> returns the counter, not ``None``).
        """
        row = self._inflight_row(started_minutes_ago=40)
        summary = {"nodeless_failed": 0, "nodeless_redispatched": 0, "nodeless_capped": 0, "skipped": 0}

        with patch.object(ch, "get_settings", return_value=MagicMock(saq_nodeless_redispatch_budget=4)):
            handled = await ch._reconcile_nodeless_repair(
                AsyncMock(),
                MagicMock(),
                uuid.uuid4(),
                row,
                35,
                0,
                summary,
                [],
            )

        assert handled is None
        assert summary["nodeless_failed"] == 0
        assert summary["nodeless_redispatched"] == 0
        assert summary["skipped"] == 0

    def test_live_marker_row_catchable_past_the_safety_floor(self) -> None:
        """Past the in-flight floor (70 min > 3900 s) the marker no longer
        shields: the row is catchable again, so a dead node-deadline watchdog
        can never strand the run forever."""
        row = self._inflight_row(started_minutes_ago=70)

        assert ch._NODELESS_IN_FLIGHT_FLOOR_SECONDS == 3900
        assert ch._is_nodeless_zombie_row(row, 35) is True

    def test_hitl_tombstone_row_is_not_shielded(self) -> None:
        """The ``cleared_at_hitl`` tombstone records only a PAST dispatch (the
        marker was cleared) — it is not a live marker, so it takes no
        in-flight shield: pre-W-B behaviour preserved."""
        row = self._inflight_row(started_minutes_ago=40)
        row.sandbox_dispatch_state = json.dumps({"state": "cleared_at_hitl", "written_at": "2026-10-01T00:00:00+00:00"})

        assert ch._is_nodeless_zombie_row(row, 35) is True

    def test_sql_predicate_carries_the_marker_leg_and_floor(self) -> None:
        """The SQL predicate (not just the row recheck) carries the in-flight
        leg: a NULL-or-tombstone marker OR age past the 3900 s floor. Fails
        without W-B: neither the marker disjunct nor the floor bind exists."""
        compiled = sa.select(Run.id).where(ch._nodeless_zombie_predicate(35)).compile()
        sql = str(compiled)

        assert "runs.sandbox_dispatch_state IS NULL" in sql
        assert '"state": "cleared_at_hitl"' in sql

        literal = (
            sa.select(Run.id)
            .where(ch._nodeless_zombie_predicate(35))
            .compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert f"now() - {ch._NODELESS_IN_FLIGHT_FLOOR_SECONDS} * interval" in str(literal)
