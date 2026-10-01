"""FAR-1088 (W6) — three confirmations the dispatch-phase slice must hold.

1. Done-means #4: a healthy, progressing run is NEVER selected by the
   claimed-but-nodeless zombie machinery — the row-level predicate rejects it,
   the SQL scan keeps its progress legs, and the stale-heartbeat repair branch
   only admits a running run once its heartbeat is stale.
2. The instrumentation columns stay internal: ``dispatch_phase`` /
   ``dispatch_phase_entered_at`` are not projected by the run API
   (``RunResponse`` schema, detail payload, or list item).
3. Every claim re-stamps ``dispatch_phase='claimed'`` (+ entry time) in the
   SAME UPDATE that claims the row, so a re-claim RESETS the phase and a
   re-dispatched run can never report a previous attempt's phase.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    started_minutes_ago: float,
    heartbeat_minutes_ago: float,
) -> SimpleNamespace:
    """A running SAQ row in the shape the reconcile scan hands to the
    row-level re-check (``outputs_absent`` is the scan's NOT EXISTS flag)."""
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        status="running",
        dispatcher="saq",
        node_token_usage=node_token_usage,
        outputs_absent=outputs_absent,
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
    """Both claim UPDATE variants stamp ``dispatch_phase='claimed'`` (and its
    entry time) in the SAME statement that claims the row — so every re-claim
    (including a re-dispatch's claim) resets the phase, and a re-woken run can
    never report a previous attempt's phase."""

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
