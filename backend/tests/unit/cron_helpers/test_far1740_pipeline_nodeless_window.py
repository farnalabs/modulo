"""FAR-1740: the claimed-but-nodeless window is derived PER-PIPELINE.

The fixed ``SAQ_CLAIMED_NODELESS_MINUTES`` (35 min) and the FAR-873 early-detect
window (15 min) are calibrated to an 1800s node. A sandbox node writes no
LangGraph checkpoint until it COMPLETES a super-step, so a pipeline whose
longest node is 3300s (55 min) is misread as a never-dispatched zombie at
15/35 min, re-dispatched with rotating ``claim_count`` and terminal-failed
``agent.stall``.

These tests pin: the derivation formula (and the mechanical
window > max-node-timeout invariant), the row-level graph extraction, the
early-detect gating for a widened window, the widened-window tail guard, and
the FAR-1740 (3) age-gate hard-in-flight exclusion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.runner_capacity import MARKER_STATE_CLEARED_AT_HITL


class TestPipelineNodelessWindowMinutes:
    """The per-pipeline window derivation: ``ceil(node/60) + margin``, clamped."""

    def test_unknown_node_timeout_returns_the_floor_unchanged(self) -> None:
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=None) == 35

    @pytest.mark.parametrize("bad", [0, -1, True, False, "1800", 12.5])
    def test_non_positive_or_non_int_returns_the_floor(self, bad: Any) -> None:
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=bad) == 35

    def test_default_1800s_node_keeps_the_historical_35_min_window(self) -> None:
        # ceil(1800/60) + 5 = 35 — exactly the documented calibration, so the
        # standard agent pipelines' behaviour is unchanged.
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=1800) == 35

    def test_3300s_node_yields_a_60_min_window(self) -> None:
        # ceil(3300/60) + 5 = 60 > 55 — the bug this ticket fixes.
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=3300) == 60

    def test_intermediate_node_timeout_derives_mid_window(self) -> None:
        # ceil(2400/60) + 5 = 45.
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=2400) == 45

    def test_window_never_shrinks_below_the_global_floor(self) -> None:
        # A pipeline-level 60s default must not shrink the window below the
        # global 35-min floor.
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=60) == 35

    def test_window_clamps_to_the_ceiling(self) -> None:
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=10_000_000) == (
            ch.NODELESS_WINDOW_MAX_MINUTES
        )

    @pytest.mark.parametrize("node_timeout", [1, 900, 1800, 2400, 3300])
    def test_window_strictly_exceeds_the_max_node_timeout(self, node_timeout: int) -> None:
        # The mechanical invariant the ticket demands.
        window = ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=node_timeout)
        assert window * 60 > node_timeout

    def test_ceiling_division_never_rounds_coverage_down(self) -> None:
        # 1801s is a hair over 30 min — the window must be 36, not 35.
        assert ch.pipeline_nodeless_window_minutes(35, max_node_timeout_seconds=1801) == 36


class TestMaxNodeTimeoutFromGraph:
    """``_max_node_timeout_seconds_from_graph`` walks dict graphs and bare lists."""

    def test_dict_graph_returns_the_largest_node_timeout(self) -> None:
        graph = {"nodes": [{"timeout_seconds": 300}, {"timeout_seconds": 3300}, {"timeout_seconds": 1200}]}
        assert ch._max_node_timeout_seconds_from_graph(graph) == 3300

    def test_bare_node_list_is_accepted(self) -> None:
        nodes = [{"timeout_seconds": 600}, {"timeout_seconds": 1800}]
        assert ch._max_node_timeout_seconds_from_graph(nodes) == 1800

    @pytest.mark.parametrize("bad", [None, "nope", 42, {"nodes": "x"}, {"nodes": [None, "y", 3.5]}])
    def test_malformed_input_degrades_to_none(self, bad: Any) -> None:
        assert ch._max_node_timeout_seconds_from_graph(bad) is None

    def test_bool_is_never_treated_as_a_timeout(self) -> None:
        assert ch._max_node_timeout_seconds_from_graph([{"timeout_seconds": True}]) is None

    def test_missing_timeout_keys_are_skipped(self) -> None:
        assert ch._max_node_timeout_seconds_from_graph([{"node_type": "agent"}, {"timeout_seconds": 900}]) == 900


class TestRowPipelineMaxNodeTimeout:
    """``_row_pipeline_max_node_timeout_seconds`` prefers the frozen snapshot."""

    def test_snapshot_graph_is_used(self) -> None:
        row = SimpleNamespace(
            snapshot_graph_json={"nodes": [{"timeout_seconds": 3300}]},
            pipeline_graph_json=[{"timeout_seconds": 1200}],
            pipeline_node_timeout_seconds=300,
        )
        # MAX across sources: over-estimating merely waits longer.
        assert ch._row_pipeline_max_node_timeout_seconds(row) == 3300

    def test_live_pipeline_graph_is_a_fallback(self) -> None:
        row = SimpleNamespace(pipeline_graph_json=[{"timeout_seconds": 2400}])
        assert ch._row_pipeline_max_node_timeout_seconds(row) == 2400

    def test_pipeline_default_is_the_last_fallback(self) -> None:
        row = SimpleNamespace(pipeline_node_timeout_seconds=600)
        assert ch._row_pipeline_max_node_timeout_seconds(row) == 600

    def test_no_graph_information_returns_none(self) -> None:
        row = SimpleNamespace(status="running", started_at=datetime.now(UTC))
        assert ch._row_pipeline_max_node_timeout_seconds(row) is None


class TestRowEarlyDetectMinutes:
    """Early-detect is disabled when the per-pipeline window was widened."""

    def test_not_widened_keeps_the_configured_early_window(self) -> None:
        assert ch._row_early_detect_minutes(15, floor_minutes=35, row_window_minutes=35) == 15

    def test_widened_window_disables_early_detect(self) -> None:
        assert ch._row_early_detect_minutes(15, floor_minutes=35, row_window_minutes=60) is None

    def test_none_early_window_stays_none(self) -> None:
        assert ch._row_early_detect_minutes(None, floor_minutes=35, row_window_minutes=60) is None


class TestZeroProgressShapeRow:
    """The row-level mirror of the zero-progress SQL shape fails closed."""

    def test_all_zero_progress_legs_true_is_zero_progress(self) -> None:
        row = SimpleNamespace(node_token_usage=None, outputs_absent=True, checkpoints_absent=True)
        assert ch._is_zero_progress_shape_row(row) is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"node_token_usage": {}},
            {"outputs_absent": False},
            {"checkpoints_absent": False},
        ],
    )
    def test_any_progress_leg_breaks_the_shape(self, overrides: dict[str, Any]) -> None:
        base = {"node_token_usage": None, "outputs_absent": True, "checkpoints_absent": True}
        base.update(overrides)
        assert ch._is_zero_progress_shape_row(SimpleNamespace(**base)) is False

    def test_missing_flags_fail_closed(self) -> None:
        assert ch._is_zero_progress_shape_row(SimpleNamespace(node_token_usage=None)) is False


class TestWidenedWindowTailGuard:
    """A zero-progress running row is held back until its per-pipeline window."""

    def _row(self, **overrides: Any) -> SimpleNamespace:
        fields: dict[str, Any] = {
            "status": "running",
            "dispatcher": "saq",
            "node_token_usage": None,
            "outputs_absent": True,
            "checkpoints_absent": True,
            "started_at": datetime.now(UTC) - timedelta(minutes=20),
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_widened_window_holds_back_a_young_zero_progress_row(self) -> None:
        row = self._row(started_at=datetime.now(UTC) - timedelta(minutes=20))
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=35) is True

    def test_widened_window_releases_once_the_window_elapsed(self) -> None:
        row = self._row(started_at=datetime.now(UTC) - timedelta(minutes=61))
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=35) is False

    def test_not_widened_never_holds_back(self) -> None:
        row = self._row(started_at=datetime.now(UTC) - timedelta(minutes=20))
        assert ch._held_back_by_widened_window(row, nodeless_window=35, floor_nodeless_window=35) is False

    def test_missing_floor_never_holds_back(self) -> None:
        row = self._row(started_at=datetime.now(UTC) - timedelta(minutes=20))
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=None) is False

    def test_progress_bearing_row_is_not_held_back(self) -> None:
        row = self._row(checkpoints_absent=False, started_at=datetime.now(UTC) - timedelta(minutes=20))
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=35) is False

    def test_non_saq_row_is_not_held_back(self) -> None:
        row = self._row(dispatcher=None, started_at=datetime.now(UTC) - timedelta(minutes=20))
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=35) is False

    def test_pending_row_is_not_held_back(self) -> None:
        row = self._row(status="pending", started_at=datetime.now(UTC) - timedelta(minutes=20))
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=35) is False

    def test_missing_started_at_is_not_held_back(self) -> None:
        row = self._row(started_at=None)
        assert ch._held_back_by_widened_window(row, nodeless_window=60, floor_nodeless_window=35) is False


class TestLiveDispatchMarkerSql:
    """FAR-1740 (3): the live/no-live marker fragments are exact complements."""

    def test_tombstone_pattern_names_the_hitl_state(self) -> None:
        pattern = ch._live_dispatch_marker_tombstone_pattern()
        assert MARKER_STATE_CLEARED_AT_HITL in pattern
        assert pattern.startswith("%")
        assert pattern.endswith("%")

    def test_no_live_marker_sql_is_the_negation(self) -> None:
        fragment = ch._nodeless_no_live_marker_sql()
        assert "IS NULL OR" in fragment
        assert MARKER_STATE_CLEARED_AT_HITL in fragment


class _CaptureSession:
    """Minimal async session capturing every executed statement."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.calls.append((str(stmt), dict(params or {})))
        result = MagicMock()
        result.all.return_value = []
        result.rowcount = 0
        return result

    async def flush(self) -> None:
        return


class TestAgeGateHardInFlightExclusion:
    """FAR-1740 (3): the age gate never abandons a node-started + live-marker row."""

    @pytest.mark.asyncio
    async def test_age_gate_sql_excludes_node_started_live_marker_rows(self) -> None:
        session = _CaptureSession()
        await ch._terminalize_mid_graph_wedges(session, uuid.uuid4(), max_age_minutes=135, max_rows=None)
        sql, params = session.calls[0]
        # The exclusion is present and the tombstone pattern travels as a BIND.
        assert "dispatch_phase = :first_node_phase" in sql
        assert "sandbox_dispatch_state IS NOT NULL" in sql
        assert "NOT LIKE :live_marker_tombstone" in sql
        assert MARKER_STATE_CLEARED_AT_HITL in params["live_marker_tombstone"]
        # The phase bind is the shared constant — one definition, both statements.
        assert params["first_node_phase"] == PHASE_FIRST_NODE_DISPATCHED

    def test_age_gate_template_carries_the_exclusion_arm(self) -> None:
        assert ":live_marker_tombstone" in ch._MID_GRAPH_WEDGE_SQL
        assert "dispatch_phase = :first_node_phase" in ch._MID_GRAPH_WEDGE_SQL

    def test_router_sql_has_no_live_marker_arm(self) -> None:
        # The router already excludes node-started rows via the zero-progress
        # shape; it must NOT carry the live-marker exclusion.
        assert ":live_marker_tombstone" not in ch._NODELESS_ROUTER_SQL


class TestReconcileOneRowTailGuard:
    """The tail guard inside ``_reconcile_one_row`` holds the widened-window row.

    The direct ``_held_back_by_widened_window`` tests above pin the predicate;
    this exercises the branch the predicate gates in the real row loop — the
    ``summary["skipped"]`` / early-return arm — so a young zero-progress row
    from a widened-window pipeline is never re-dispatched by the generic
    recovery path before its per-pipeline window elapses.
    """

    @pytest.mark.asyncio
    async def test_widened_window_row_is_held_back_before_the_generic_path(self) -> None:
        row = SimpleNamespace(
            id=uuid.uuid4(),
            status="running",
            dispatcher="saq",
            node_token_usage=None,
            outputs_absent=True,
            checkpoints_absent=True,
            started_at=datetime.now(UTC) - timedelta(minutes=20),
        )
        summary: dict[str, Any] = {"skipped": 0}
        # session/queue/redis are unused: the early-detect block is off
        # (early_detect_minutes defaults to None), the nodeless repair returns
        # None (the row is younger than the full window so it is not a zombie),
        # and the tail guard returns before any of them is touched.
        got = await ch._reconcile_one_row(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            uuid.uuid4(),
            row,
            60,
            0,
            summary,
            [],
            floor_nodeless_window=35,
        )
        assert got == 0
        assert summary["skipped"] == 1

    @pytest.mark.asyncio
    async def test_non_widened_row_falls_through_past_the_tail_guard(self) -> None:
        # Same row but the window was NOT widened (window == floor): the tail
        # guard must not fire, so the row continues down the generic recovery
        # path (patched to a sentinel here) rather than being held back.
        row = SimpleNamespace(
            id=uuid.uuid4(),
            status="running",
            dispatcher="saq",
            node_token_usage=None,
            outputs_absent=True,
            checkpoints_absent=True,
            started_at=datetime.now(UTC) - timedelta(minutes=20),
        )
        summary: dict[str, Any] = {"skipped": 0}
        with (
            patch.object(ch, "_reconcile_enqueue_failed", new_callable=AsyncMock, return_value=7),
        ):
            got = await ch._reconcile_one_row(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                uuid.uuid4(),
                row,
                35,
                0,
                summary,
                [],
                floor_nodeless_window=35,
            )
        assert got == 7
        assert summary["skipped"] == 0

    def test_orm_nodeless_predicate_still_compiles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.db.models.run import Run

        monkeypatch.setattr(ch, "get_settings", lambda: MagicMock())
        compiled = sa.select(Run.id).where(ch._nodeless_zombie_predicate(60)).compile()
        assert "started_at" in str(compiled)
