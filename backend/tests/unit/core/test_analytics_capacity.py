"""Unit tests for the capacity / queue-pressure report (FAR-1343).

Pure, no DB: ``compute_capacity_report`` and its helpers take already-fetched
rows, so the metric contract is pinned without a session. The scenarios cover
the two questions the ticket names — queue draining / which limit binds, and
org active-run health (the leak/stall signature) — plus the metric-correctness
rules the change is specifically about (parked runs are not "executing"; a run
cancelled before it ever started stops counting as queued; the partial trailing
interval never reads as "nothing completed").
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from modulo.core.analytics import capacity as capacity_module
from modulo.core.analytics.capacity import (
    CAPACITY_MAX_RAW_ROWS,
    CAUSE_ORG_RUN,
    CAUSE_PIPELINE,
    CAUSE_SANDBOX,
    CAUSE_UNMARKED,
    CapacityParams,
    _bucket_boundaries,
    _cause_of,
    _depth_at,
    _depth_series,
    _field,
    _heartbeat_reference,
    _queued_intervals,
    _sandbox_state,
    build_capacity_health_query,
    build_capacity_rows_query,
    build_pipeline_meta_query,
    compute_capacity_report,
    run_capacity_query,
    validate_capacity_params,
)
from modulo.core.analytics.service import (
    AnalyticsDatabaseError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
)

_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
_WINDOW_START = _NOW - timedelta(minutes=60)

_P1 = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_P2 = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _row(
    *,
    pipeline_id: uuid.UUID = _P1,
    status: str = "pending",
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    heartbeat_at: datetime | None = None,
    cancellation_requested: bool = False,
) -> dict[str, Any]:
    return {
        "pipeline_id": pipeline_id,
        "status": status,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "error_code": error_code,
        "error_detail": error_detail,
        "heartbeat_at": heartbeat_at,
        "cancellation_requested": cancellation_requested,
    }


def _minutes_before(now: datetime, minutes: float) -> datetime:
    return now - timedelta(minutes=minutes)


def _compute(
    rows: list[Any],
    *,
    now: datetime = _NOW,
    window_start: datetime = _WINDOW_START,
    interval_seconds: int = 300,
    stall_intervals: int = 3,
    org_run_limit: int | None = None,
    sandbox_cap: int | None = None,
    sandbox_active: int = 0,
    pipelines: list[dict[str, Any]] | None = None,
    health_rows: list[Any] | None = None,
    visible_pipeline_ids: set[str] | None = None,
) -> dict[str, Any]:
    health_kwargs: dict[str, Any] = {} if health_rows is None else {"health_rows": health_rows}
    return compute_capacity_report(
        rows=rows,
        pipelines=pipelines
        if pipelines is not None
        else [
            {"id": _P1, "name": "alpha", "max_concurrent_runs": 5},
            {"id": _P2, "name": "beta", "max_concurrent_runs": 5},
        ],
        org_run_limit=org_run_limit,
        sandbox_cap=sandbox_cap,
        sandbox_is_default=False,
        sandbox_active=sandbox_active,
        now=now,
        window_start=window_start,
        interval_seconds=interval_seconds,
        stall_intervals=stall_intervals,
        stale_heartbeat_seconds=1800,
        visible_pipeline_ids=visible_pipeline_ids,
        **health_kwargs,
    )


class TestValidateParams:
    def test_defaults_are_accepted(self) -> None:
        params = validate_capacity_params(CapacityParams())
        assert params.window_minutes == 60

    def test_window_below_minimum_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError):
            validate_capacity_params(CapacityParams(window_minutes=4))

    def test_interval_below_minimum_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError):
            validate_capacity_params(CapacityParams(interval_seconds=30))

    def test_stall_intervals_below_one_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError):
            validate_capacity_params(CapacityParams(stall_intervals=0))

    def test_interval_longer_than_window_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError):
            validate_capacity_params(CapacityParams(window_minutes=5, interval_seconds=600))

    def test_max_raw_rows_constant_is_positive(self) -> None:
        assert CAPACITY_MAX_RAW_ROWS > 0

    def test_bucket_count_above_maximum_is_rejected(self) -> None:
        # Regression (FAR-1343 M4): 1440 minutes at a 60s interval is 1,440
        # buckets — the unbounded O(buckets x rows) build a caller could force.
        with pytest.raises(AnalyticsValidationError):
            validate_capacity_params(CapacityParams(window_minutes=1440, interval_seconds=60))

    def test_bucket_count_at_maximum_is_accepted(self) -> None:
        bucket_count = (1440 * 60) // 180
        assert bucket_count == capacity_module.MAX_CAPACITY_BUCKETS
        params = validate_capacity_params(CapacityParams(window_minutes=1440, interval_seconds=180))
        assert params.interval_seconds == 180

    def test_max_capacity_buckets_constant_is_positive(self) -> None:
        assert capacity_module.MAX_CAPACITY_BUCKETS > 0


class TestCauseClassification:
    def test_pipeline_marker_maps_to_pipeline(self) -> None:
        assert _cause_of(_row(error_code="pipeline_capacity")) == CAUSE_PIPELINE

    def test_org_marker_with_sandbox_detail_maps_to_sandbox(self) -> None:
        row = _row(
            error_code="org_capacity_limited",
            error_detail="Org sandbox concurrency limit reached: 4 active, cap 4",
        )
        assert _cause_of(row) == CAUSE_SANDBOX

    def test_org_marker_with_org_run_detail_maps_to_org_run(self) -> None:
        row = _row(
            error_code="org_capacity_limited",
            error_detail="Org run concurrency limit reached: 5 active, cap 5",
        )
        assert _cause_of(row) == CAUSE_ORG_RUN

    def test_org_marker_with_unknown_detail_maps_to_org_run(self) -> None:
        assert _cause_of(_row(error_code="org_capacity_limited", error_detail="something else")) == CAUSE_ORG_RUN

    def test_no_marker_maps_to_unmarked(self) -> None:
        assert _cause_of(_row()) == CAUSE_UNMARKED


class TestHelpers:
    def test_bucket_boundaries_cover_window_inclusively(self) -> None:
        bounds = _bucket_boundaries(_WINDOW_START, _NOW, 300)
        assert bounds[0] == _WINDOW_START
        assert bounds[-1] == _NOW
        # 60m / 5m = 12 intervals; the grid runs 11:00..11:55 and appends now.
        assert len(bounds) == 13

    def test_depth_at_returns_count_and_oldest_age(self) -> None:
        intervals: list[tuple[datetime, datetime | None]] = [
            (_NOW - timedelta(minutes=30), None),
            (_NOW - timedelta(minutes=5), None),
        ]
        depth, oldest = _depth_at(intervals, _NOW)
        assert depth == 2
        assert oldest == 1800.0

    def test_depth_at_half_open_interval(self) -> None:
        start = _NOW - timedelta(minutes=10)
        depth, oldest = _depth_at([(start, _NOW)], _NOW)
        assert depth == 0
        assert oldest is None

    def test_depth_at_shared_start_with_open_and_closed_end_does_not_raise(self) -> None:
        # Regression (FAR-1343 C1): two queue entries sharing ``created_at``
        # where one is still open (end=None) and one has started. The old
        # min-heap pushed ``(start, end)`` tuples, so on an equal start Python
        # compared the second element and raised
        # ``TypeError: '<' not supported between 'datetime.datetime' and 'NoneType'``.
        shared = _NOW - timedelta(minutes=20)
        intervals: list[tuple[datetime, datetime | None]] = [
            (shared, None),
            (shared, _NOW + timedelta(minutes=5)),
        ]
        depth, oldest = _depth_at(intervals, _NOW)
        assert depth == 2
        assert oldest == 1200.0

    def test_heartbeat_reference_falls_back_to_started_then_created(self) -> None:
        started = _NOW - timedelta(minutes=5)
        created = _NOW - timedelta(minutes=10)
        beat = _heartbeat_reference(_row(started_at=started, created_at=created), _NOW)
        assert beat == started
        no_start = _heartbeat_reference(_row(created_at=created), _NOW)
        assert no_start == created


class TestQueueReport:
    def test_pending_depth_age_cause_and_drain(self) -> None:
        rows = [
            # Two currently-pending rows (one unmarked, one pipeline-marked).
            _row(created_at=_minutes_before(_NOW, 30)),
            _row(created_at=_minutes_before(_NOW, 10), error_code="pipeline_capacity"),
            # Executing + terminal rows supply the drain counts.
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
                heartbeat_at=_NOW,
            ),
            _row(
                status="complete",
                created_at=_minutes_before(_NOW, 50),
                started_at=_minutes_before(_NOW, 48),
                completed_at=_minutes_before(_NOW, 30),
            ),
            _row(
                status="complete",
                created_at=_minutes_before(_NOW, 20),
                started_at=_minutes_before(_NOW, 15),
                completed_at=_minutes_before(_NOW, 5),
            ),
            # Cancelled before it ever started: queued only until completed_at.
            _row(status="cancelled", created_at=_minutes_before(_NOW, 40), completed_at=_minutes_before(_NOW, 35)),
        ]
        report = _compute(rows)
        assert report["pending"]["total"] == 2
        assert report["pending"]["oldest_pending_age_seconds"] == 1800.0
        assert report["pending"]["by_cause"][CAUSE_UNMARKED] == 1
        assert report["pending"]["by_cause"][CAUSE_PIPELINE] == 1
        assert report["drain"]["created"] == 6
        assert report["drain"]["started"] == 3
        assert report["drain"]["completed"] == 3
        assert report["drain"]["ratio"] == 0.5
        assert report["drain"]["verdict"] == "backing_up"

    def test_cancelled_never_started_row_does_not_inflate_history(self) -> None:
        # A cancelled run created and terminated inside the window queued only
        # for its 5-minute life, so the queue is empty again by now.
        rows = [
            _row(status="cancelled", created_at=_minutes_before(_NOW, 40), completed_at=_minutes_before(_NOW, 35)),
        ]
        report = _compute(rows)
        assert report["pending"]["total"] == 0
        assert report["series"][-1]["pending_depth"] == 0

    def test_cancellation_requested_pending_excluded_from_depth(self) -> None:
        rows = [_row(created_at=_minutes_before(_NOW, 30), cancellation_requested=True)]
        report = _compute(rows)
        assert report["pending"]["total"] == 0

    def test_series_is_zero_filled_and_drain_per_bucket(self) -> None:
        rows = [
            _row(
                status="complete",
                created_at=_minutes_before(_NOW, 20),
                started_at=_minutes_before(_NOW, 18),
                completed_at=_minutes_before(_NOW, 10),
            ),
        ]
        report = _compute(rows)
        assert len(report["series"]) == 12
        last = report["series"][-1]
        assert last["complete"] is False
        created_buckets = [bucket for bucket in report["series"] if bucket["created"] == 1]
        assert len(created_buckets) == 1

    def test_series_by_pipeline_reports_per_pipeline_depth(self) -> None:
        rows = [
            _row(pipeline_id=_P1, created_at=_minutes_before(_NOW, 30)),
            _row(pipeline_id=_P2, created_at=_minutes_before(_NOW, 10)),
        ]
        report = _compute(rows)
        final = report["series"][-1]
        by_id = {entry["pipeline_id"]: entry for entry in final["by_pipeline"]}
        assert by_id[str(_P1)]["pending_depth"] == 1
        assert by_id[str(_P2)]["pending_depth"] == 1
        assert by_id[str(_P1)]["oldest_pending_age_seconds"] == 1800.0

    def test_empty_queue_verdict_is_draining(self) -> None:
        report = _compute([])
        assert report["drain"]["verdict"] == "draining"
        assert report["pending"]["oldest_pending_age_seconds"] is None

    def test_shared_created_at_started_and_pending_row_builds_series(self) -> None:
        # Regression (FAR-1343 C1) at the report level: two runs sharing
        # ``created_at`` (one pending, one already started) is the shape
        # ``server_default=func.current_timestamp()`` + a multi-run transaction
        # produces. The series build must not raise, and the pre-start slices
        # must show both entries queued.
        created = _minutes_before(_NOW, 20)
        rows = [
            _row(status="pending", created_at=created),
            _row(
                status="running",
                created_at=created,
                started_at=_minutes_before(_NOW, 15),
                heartbeat_at=_NOW,
            ),
        ]
        report = _compute(rows)
        assert max(bucket["pending_depth"] for bucket in report["series"]) == 2
        assert report["series"][-1]["pending_depth"] == 1


class TestActiveHealth:
    def test_parked_runs_are_not_counted_as_executing(self) -> None:
        rows = [
            _row(status="awaiting_human", created_at=_minutes_before(_NOW, 30), started_at=_minutes_before(_NOW, 25)),
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 10),
                started_at=_minutes_before(_NOW, 8),
                heartbeat_at=_NOW,
            ),
        ]
        report = _compute(rows, org_run_limit=10)
        health = report["active_health"]
        assert health["gate_total"] == 2
        assert health["executing_total"] == 1
        assert health["parked_total"] == 1
        assert health["running_total"] == 1
        assert health["utilisation"] == pytest.approx(0.2)

    def test_cap_held_with_zero_completions_is_a_stall(self) -> None:
        rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
                heartbeat_at=_NOW,
            ),
            _row(
                pipeline_id=_P2,
                status="running",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
                heartbeat_at=_NOW,
            ),
            _row(
                created_at=_minutes_before(_NOW, 20),
                error_code="org_capacity_limited",
                error_detail="Org run concurrency limit reached: 2 active, cap 2",
            ),
        ]
        report = _compute(
            rows,
            window_start=_NOW - timedelta(minutes=30),
            interval_seconds=300,
            stall_intervals=3,
            org_run_limit=2,
        )
        health = report["active_health"]
        assert health["cap_held"] is True
        assert health["completions_in_window"] == 0
        assert health["zero_completion_intervals"] >= 3
        assert health["stall_suspected"] is True
        assert report["drain"]["verdict"] == "stalled"

    def test_cap_held_entirely_by_parked_runs_is_not_a_stall(self) -> None:
        # Regression (FAR-1343 M2): ``gate_total`` counts human-waiting rows
        # (the org admission gate holds their slot), so the stall signature must
        # not fire when NO run is actually executing. A cap held solely by
        # awaiting_human/hitl_parked runs is not a leaked-slot stall.
        rows = [
            _row(
                status="awaiting_human",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
            ),
            _row(
                pipeline_id=_P2,
                status="hitl_parked",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
            ),
        ]
        report = _compute(
            rows,
            window_start=_NOW - timedelta(minutes=30),
            interval_seconds=300,
            stall_intervals=3,
            org_run_limit=2,
        )
        health = report["active_health"]
        assert health["gate_total"] == 2
        assert health["executing_total"] == 0
        assert health["parked_total"] == 2
        assert health["cap_held"] is True
        assert health["stall_suspected"] is False
        assert report["drain"]["verdict"] != "stalled"

    def test_completions_prevent_a_stall(self) -> None:
        rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
                heartbeat_at=_NOW,
            ),
            _row(
                pipeline_id=_P2,
                status="running",
                created_at=_minutes_before(_NOW, 60),
                started_at=_minutes_before(_NOW, 55),
                heartbeat_at=_NOW,
            ),
            _row(
                status="complete",
                created_at=_minutes_before(_NOW, 25),
                started_at=_minutes_before(_NOW, 24),
                completed_at=_minutes_before(_NOW, 12),
            ),
        ]
        report = _compute(
            rows,
            window_start=_NOW - timedelta(minutes=30),
            interval_seconds=300,
            stall_intervals=3,
            org_run_limit=2,
        )
        health = report["active_health"]
        assert health["cap_held"] is True
        assert health["stall_suspected"] is False

    def test_stale_heartbeat_is_counted_and_attributed(self) -> None:
        rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 120),
                started_at=_minutes_before(_NOW, 110),
                heartbeat_at=_minutes_before(_NOW, 60),
            ),
        ]
        report = _compute(rows)
        health = report["active_health"]
        assert health["stale_heartbeat_count"] == 1
        assert health["attribution"][0]["stale_heartbeat_count"] == 1
        assert health["attribution"][0]["running"] == 1

    def test_cancellation_requested_running_counted_separately(self) -> None:
        rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 10),
                started_at=_minutes_before(_NOW, 5),
                cancellation_requested=True,
            ),
        ]
        report = _compute(rows)
        health = report["active_health"]
        assert health["gate_total"] == 0
        assert health["cancelling_total"] == 1

    def test_running_row_without_started_at_has_no_executing_age(self) -> None:
        # A running row that has not stamped started_at yet still counts as
        # executing but contributes no oldest-age (and beats via heartbeat).
        rows = [_row(status="running", created_at=_minutes_before(_NOW, 10), heartbeat_at=_NOW)]
        report = _compute(rows)
        att = report["active_health"]["attribution"][0]
        assert att["running"] == 1
        assert att["oldest_executing_age_seconds"] is None

    def test_org_health_uses_org_wide_rows_not_team_scoped(self) -> None:
        # Regression (FAR-1343 M3): the active-health denominator
        # (``org_run_limit``) is org-wide. A team viewer whose ``rows`` are
        # scoped to their own team must still see the ORG's utilisation and
        # cap_held from the org-wide health rows — never their team's handful of
        # runs divided by the whole org's cap.
        team_rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            ),
            _row(created_at=_minutes_before(_NOW, 20)),
        ]
        org_rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            )
        ] + [
            _row(
                pipeline_id=_P2,
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            )
            for _ in range(4)
        ]
        report = _compute(team_rows, org_run_limit=5, health_rows=org_rows)
        health = report["active_health"]
        assert health["gate_total"] == 5
        assert health["executing_total"] == 5
        assert health["utilisation"] == 1.0
        assert health["cap_held"] is True
        # The queue section stays team-scoped: only the caller's pending run.
        assert report["pending"]["total"] == 1

    def test_team_scope_filters_attribution_but_keeps_org_wide_totals(self) -> None:
        # Regression (FAR-1343 M5): a team viewer's ``rows`` are team-scoped,
        # but the health TOTALS are org-wide (M3) — and the previous code fed
        # those org-wide rows straight into the attribution breakdown, so the
        # viewer received a foreign team's live executing count and oldest age.
        # The fix restricts ``attribution`` to the caller-visible pipeline set
        # while leaving every org-wide total untouched.
        team_rows = [
            _row(
                pipeline_id=_P1,
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            )
        ]
        org_rows = list(team_rows) + [
            _row(
                pipeline_id=_P2,  # a pipeline OUTSIDE the caller's team
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            )
            for _ in range(3)
        ]
        report = _compute(
            team_rows,
            org_run_limit=4,
            health_rows=org_rows,
            pipelines=[{"id": _P1, "name": "alpha", "max_concurrent_runs": 5}],
            visible_pipeline_ids={str(_P1)},
        )
        health = report["active_health"]
        # Org-wide totals are unchanged by the attribution scope.
        assert health["gate_total"] == 4
        assert health["executing_total"] == 4
        assert health["running_total"] == 4
        assert health["utilisation"] == 1.0
        assert health["cap_held"] is True
        # Attribution names only the caller-visible pipeline.
        assert [a["pipeline_id"] for a in health["attribution"]] == [str(_P1)]
        assert str(_P2) not in {a["pipeline_id"] for a in health["attribution"]}

    def test_team_scope_filter_keeps_stale_heartbeat_total_org_wide(self) -> None:
        # The heartbeat aggregate is a NUMBER, so it stays org-wide like the
        # other totals even though the per-pipeline attribution is filtered.
        stale_start = _minutes_before(_NOW, 120)
        stale_beat = _minutes_before(_NOW, 60)
        org_rows = [
            _row(
                pipeline_id=_P1,
                status="running",
                created_at=stale_start,
                started_at=stale_start,
                heartbeat_at=stale_beat,
            ),
            _row(
                pipeline_id=_P2,
                status="running",
                created_at=stale_start,
                started_at=stale_start,
                heartbeat_at=stale_beat,
            ),
        ]
        report = _compute(
            [org_rows[0]],
            health_rows=org_rows,
            pipelines=[{"id": _P1, "name": "alpha", "max_concurrent_runs": 5}],
            visible_pipeline_ids={str(_P1)},
        )
        health = report["active_health"]
        assert health["stale_heartbeat_count"] == 2
        assert [a["pipeline_id"] for a in health["attribution"]] == [str(_P1)]


class TestBinding:
    def test_org_run_cap_binds_when_held(self) -> None:
        rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            ),
            _row(
                created_at=_minutes_before(_NOW, 20),
                error_code="org_capacity_limited",
                error_detail="Org run concurrency limit reached: 1 active, cap 1",
            ),
        ]
        report = _compute(rows, org_run_limit=1)
        binding = report["binding"]
        assert binding["scope"] == "org_run"
        assert binding["in_use"] == 1
        assert binding["headroom"] == 0
        assert binding["admissible_if_raised"] == 1
        assert report["queue_if_raised"]["pending_after_raise"] == 0

    def test_pipeline_cap_binds_when_pipeline_at_cap(self) -> None:
        rows = [
            _row(
                status="running",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                heartbeat_at=_NOW,
            ),
            _row(status="claimed", created_at=_minutes_before(_NOW, 30), started_at=_minutes_before(_NOW, 25)),
            _row(created_at=_minutes_before(_NOW, 20), error_code="pipeline_capacity"),
        ]
        report = _compute(rows, pipelines=[{"id": _P1, "name": "alpha", "max_concurrent_runs": 2}])
        binding = report["binding"]
        assert binding["scope"] == "pipeline"
        assert binding["limit"] == 2
        assert binding["pending_blocked"] == 1

    def test_sandbox_cap_binds_before_org_run(self) -> None:
        rows = [
            _row(
                created_at=_minutes_before(_NOW, 20),
                error_code="org_capacity_limited",
                error_detail="Org sandbox concurrency limit reached: 4 active, cap 4",
            ),
        ]
        report = _compute(rows, sandbox_cap=4, sandbox_active=4, org_run_limit=10)
        binding = report["binding"]
        assert binding["scope"] == "sandbox"
        assert binding["limit"] == 4
        assert report["pending"]["by_cause"][CAUSE_SANDBOX] == 1

    def test_no_limit_binds_when_headroom_exists(self) -> None:
        rows = [_row(created_at=_minutes_before(_NOW, 20))]
        report = _compute(rows, org_run_limit=10)
        assert report["binding"]["scope"] == "none"
        assert report["binding"]["admissible_if_raised"] == 0

    def test_limits_and_pipeline_headroom_are_reported(self) -> None:
        rows = [_row(created_at=_minutes_before(_NOW, 5))]
        report = _compute(rows, org_run_limit=7, pipelines=[{"id": _P1, "name": "alpha", "max_concurrent_runs": 3}])
        assert report["limits"]["org_run_concurrency_limit"] == 7
        assert report["pending"]["by_pipeline"][0]["max_concurrent_runs"] == 3
        assert report["pending"]["by_pipeline"][0]["pipeline_name"] == "alpha"


def _compiled_scope_sql(params: CapacityParams) -> str:
    return str(build_capacity_rows_query(_P1, params, window_start=_WINDOW_START).compile(dialect=postgresql.dialect()))


class TestTeamScopePredicate:
    """The team boundary is resolved from the RUN's stamped team (with the
    pipeline owner as fallback), never the pipeline alone.

    Regression (FAR-1343): the global soft-delete filter injects
    ``pipelines.deleted_at IS NULL`` into the ON clause of the capacity query's
    OUTER JOIN, so a SOFT-DELETED pipeline yields NULL pipeline columns. The
    pre-fix predicate (``Pipeline.owner_team_id IS NULL OR IN (...)``) then
    matched the ``IS NULL`` arm and reclassified a foreign pipeline as
    org-level, leaking its surviving pending runs into a team caller's queue.
    ``coalesce(Run.owner_team_id, Pipeline.owner_team_id)`` scopes on the run's
    own stamped team, so the boundary no longer depends on the outer join.
    """

    def test_scoped_predicate_uses_the_run_stamped_team(self) -> None:
        sql = _compiled_scope_sql(CapacityParams(team_ids=(_P1,)))
        assert "coalesce(runs.owner_team_id, pipelines.owner_team_id)" in sql

    def test_scoped_predicate_has_no_bare_pipeline_null_arm(self) -> None:
        # The soft-delete-degraded `pipelines.owner_team_id IS NULL` arm is the
        # exact clause that reclassified a deleted foreign pipeline.
        sql = _compiled_scope_sql(CapacityParams(team_ids=(_P1,)))
        assert "pipelines.owner_team_id IS NULL" not in sql
        assert "coalesce(runs.owner_team_id, pipelines.owner_team_id) IS NULL" in sql

    def test_empty_team_fails_closed_on_the_coalesced_owner(self) -> None:
        sql = _compiled_scope_sql(CapacityParams(team_ids=()))
        assert "coalesce(runs.owner_team_id, pipelines.owner_team_id) IS NULL" in sql
        assert "pipelines.owner_team_id IS NULL" not in sql
        assert "coalesce(runs.owner_team_id, pipelines.owner_team_id) IN (" not in sql

    def test_none_team_is_unconstrained_and_does_not_join_pipelines(self) -> None:
        sql = _compiled_scope_sql(CapacityParams(team_ids=None))
        assert "coalesce" not in sql
        assert "LEFT OUTER JOIN" not in sql


def _compile(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class TestQueryBuilders:
    """The query builders' optional-filter arms, exercised without a DB."""

    def test_rows_query_applies_pipeline_filter(self) -> None:
        sql = _compile(build_capacity_rows_query(_P1, CapacityParams(pipeline_ids=(_P2,)), window_start=_WINDOW_START))
        assert "runs.pipeline_id IN" in sql

    def test_rows_query_without_pipeline_filter_has_no_pipeline_predicate(self) -> None:
        sql = _compile(build_capacity_rows_query(_P1, CapacityParams(), window_start=_WINDOW_START))
        assert "runs.pipeline_id IN" not in sql

    def test_pipeline_meta_query_unconstrained_team_is_org_wide(self) -> None:
        sql = _compile(build_pipeline_meta_query(_P1, CapacityParams(team_ids=None)))
        assert "owner_team_id" not in sql

    def test_pipeline_meta_query_empty_team_fails_closed(self) -> None:
        sql = _compile(build_pipeline_meta_query(_P1, CapacityParams(team_ids=())))
        assert "pipelines.owner_team_id IS NULL" in sql

    def test_pipeline_meta_query_scopes_to_own_teams(self) -> None:
        sql = _compile(build_pipeline_meta_query(_P1, CapacityParams(team_ids=(_P2,))))
        assert "pipelines.owner_team_id IS NULL" in sql
        assert "pipelines.owner_team_id IN" in sql

    def test_pipeline_meta_query_applies_pipeline_filter(self) -> None:
        sql = _compile(build_pipeline_meta_query(_P1, CapacityParams(pipeline_ids=(_P2,))))
        assert "pipelines.id IN" in sql

    def test_health_query_is_org_wide_without_a_team_join(self) -> None:
        sql = _compile(build_capacity_health_query(_P1, window_start=_WINDOW_START))
        assert "JOIN" not in sql
        assert "completed_at" in sql


class TestHelperEdges:
    """The helper arms the report-level scenarios never reach."""

    def test_field_reads_object_attributes_not_only_dicts(self) -> None:
        row = SimpleNamespace(status="running")
        assert _field(row, "status") == "running"
        assert _field(row, "absent") is None

    def test_queued_intervals_skip_missing_and_future_created_at(self) -> None:
        rows = [
            _row(created_at=None),
            _row(created_at=_NOW + timedelta(minutes=5)),
            _row(created_at=_minutes_before(_NOW, 10)),
        ]
        intervals = _queued_intervals(rows, now=_NOW)
        assert sum(len(items) for items in intervals.values()) == 1

    def test_depth_series_with_no_instants_is_empty(self) -> None:
        assert _depth_series([(_WINDOW_START, None)], []) == []

    def test_heartbeat_reference_for_missing_row_is_none(self) -> None:
        assert _heartbeat_reference(None, _NOW) is None

    def test_draining_verdict_from_a_started_surge(self) -> None:
        # A pending row older than the window keeps depth_change at 0; runs that
        # started inside the window but were created before it make the drain
        # ratio >= 1, so the verdict is "draining".
        rows = [
            _row(created_at=_minutes_before(_NOW, 90)),
            _row(
                status="complete",
                created_at=_minutes_before(_NOW, 30),
                started_at=_minutes_before(_NOW, 25),
                completed_at=_minutes_before(_NOW, 10),
            ),
        ]
        report = _compute(rows)
        assert report["drain"]["baseline_depth"] == 1
        assert report["drain"]["depth_change"] == 0
        assert report["drain"]["ratio"] >= 1
        assert report["drain"]["verdict"] == "draining"

    def test_stable_verdict_when_depth_and_drain_are_flat(self) -> None:
        # A pending row older than the window yields no in-window creations
        # (ratio None) and no depth change, so the verdict is "stable".
        rows = [_row(created_at=_minutes_before(_NOW, 90))]
        report = _compute(rows)
        assert report["drain"]["ratio"] is None
        assert report["drain"]["verdict"] == "stable"

    def test_pipeline_without_metadata_is_ignored_for_binding(self) -> None:
        # A pending run whose pipeline is not in the meta set has no cap, so it
        # can never be the binding pipeline (its metadata is None).
        rows = [_row(pipeline_id=_P2, created_at=_minutes_before(_NOW, 20), error_code="pipeline_capacity")]
        report = _compute(rows, pipelines=[{"id": _P1, "name": "alpha", "max_concurrent_runs": 5}])
        entry = report["pending"]["by_pipeline"][0]
        assert entry["max_concurrent_runs"] is None
        assert report["binding"]["scope"] == "none"


def _execute_result(rows: list[Any]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def _capacity_session(execute_results: list[list[Any]]) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_execute_result(rows) for rows in execute_results])
    return session


class TestRunCapacityQuery:
    """The bounded session service: happy path and every typed failure arm."""

    async def _run(
        self,
        session: MagicMock | None = None,
        *,
        team_ids: tuple[uuid.UUID, ...] | None = None,
        rate_limited: bool = False,
        sandbox_state: Any = (None, False, 0),
        sandbox_error: BaseException | None = None,
    ) -> dict[str, Any]:
        @contextlib.asynccontextmanager
        async def fake_guarded_session(*_args: Any, **_kwargs: Any):
            assert session is not None
            yield session

        state_mock = AsyncMock(return_value=sandbox_state)
        if sandbox_error is not None:
            state_mock = AsyncMock(side_effect=sandbox_error)
        with (
            patch.object(capacity_module, "_guarded_session", fake_guarded_session),
            patch.object(capacity_module, "_rate_limited", return_value=rate_limited),
            patch.object(capacity_module, "get_org_run_concurrency_limit", AsyncMock(return_value=10)),
            patch.object(capacity_module, "_sandbox_state", state_mock),
        ):
            return await run_capacity_query(
                org_id=_P1,
                params=CapacityParams(team_ids=team_ids),
                factory=MagicMock(),
                settings=MagicMock(),
            )

    async def test_happy_path_returns_report(self) -> None:
        rows = [_row(created_at=_minutes_before(_NOW, 20))]
        session = _capacity_session([rows, rows, [{"id": _P1, "name": "alpha", "max_concurrent_runs": 5}]])
        report = await self._run(session)
        assert report["pending"]["total"] == 1
        assert report["active_health"]["org_run_limit"] == 10

    async def test_team_scope_limits_attribution_to_the_visible_set(self) -> None:
        rows = [_row(created_at=_minutes_before(_NOW, 20))]
        session = _capacity_session([rows, rows, [{"id": _P1, "name": "alpha", "max_concurrent_runs": 5}]])
        report = await self._run(session, team_ids=(_P2,))
        assert report["active_health"]["attribution"] == []

    async def test_rate_limited_before_opening_a_session(self) -> None:
        with pytest.raises(AnalyticsRateLimitedError):
            await self._run(MagicMock(), rate_limited=True)

    async def test_row_scan_over_cap_is_rejected(self) -> None:
        over = [_row(created_at=_minutes_before(_NOW, 20))] * (CAPACITY_MAX_RAW_ROWS + 1)
        session = _capacity_session([over])
        with pytest.raises(AnalyticsValidationError):
            await self._run(session)

    async def test_health_scan_over_cap_is_rejected(self) -> None:
        one = [_row(created_at=_minutes_before(_NOW, 20))]
        over = one * (CAPACITY_MAX_RAW_ROWS + 1)
        session = _capacity_session([one, over])
        with pytest.raises(AnalyticsValidationError):
            await self._run(session)

    async def test_database_error_passthrough(self) -> None:
        session = _capacity_session([[_row(created_at=_minutes_before(_NOW, 20))]])
        with pytest.raises(AnalyticsDatabaseError):
            await self._run(session, sandbox_error=AnalyticsDatabaseError("down"))

    async def test_unexpected_error_maps_to_database_error(self) -> None:
        session = _capacity_session([[_row(created_at=_minutes_before(_NOW, 20))]])
        with pytest.raises(AnalyticsDatabaseError):
            await self._run(session, sandbox_error=RuntimeError("boom"))


class TestSandboxState:
    async def test_absent_cap_reports_none_and_zero_active(self) -> None:
        with (
            patch(
                "modulo.core.runner_capacity.read_runner_cap_contract",
                AsyncMock(return_value=(None, True)),
            ),
            patch(
                "modulo.db.crud.run.get_sandbox_concurrency_limit",
                AsyncMock(return_value=SimpleNamespace(is_default=True)),
            ),
        ):
            cap, is_default, active = await _sandbox_state(MagicMock(), _P1)
        assert (cap, is_default, active) == (None, True, 0)

    async def test_present_cap_counts_active_dispatches(self) -> None:
        with (
            patch(
                "modulo.core.runner_capacity.read_runner_cap_contract",
                AsyncMock(return_value=(4, False)),
            ),
            patch(
                "modulo.db.crud.run.get_sandbox_concurrency_limit",
                AsyncMock(return_value=SimpleNamespace(is_default=False)),
            ),
            patch(
                "modulo.core.runner_capacity.count_active_runner_dispatches_for_decision",
                AsyncMock(return_value=3),
            ),
        ):
            cap, is_default, active = await _sandbox_state(MagicMock(), _P1)
        assert (cap, is_default, active) == (4, False, 3)
