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

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

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
    _heartbeat_reference,
    compute_capacity_report,
    validate_capacity_params,
)
from modulo.core.analytics.service import AnalyticsValidationError

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
