"""Unit tests for the FAR-1344 capacity alerting sweep.

Covers the pure ladder decisions (queue rung, saturation sustain, the
dedup/escalation/auto-clear state machine), the per-pipeline trend read from the
FAR-1343 report series, payload completeness against the render templates, and
the sweep end-to-end with a fake Redis state store, a fake notifier and a
stubbed capacity reader — so a climbing queue is proven to produce exactly one
alert per escalation and a recovery on drain, without a DB.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core import capacity_alerts as ca
from modulo.core.notifier.event_mapper import _BODY_TEMPLATES, _TITLE_TEMPLATES

_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async Redis double for the dedup/escalation/auto-clear state."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expires: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.kv[key] = value
        if ex is not None:
            self.expires[key] = ex

    async def delete(self, key: str) -> None:
        self.kv.pop(key, None)
        self.sets.pop(key, None)

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


class FakeNotifier:
    """Records every dispatched event instead of delivering it."""

    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, str, dict[str, Any]]] = []

    async def dispatch_event(self, org_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> list[Any]:
        self.events.append((org_id, event_type, payload))
        return []


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------


def _report(
    *,
    pending_by_pipeline: list[dict[str, Any]] | None = None,
    series: list[dict[str, Any]] | None = None,
    binding: dict[str, Any] | None = None,
    pending_total: int | None = None,
    stall: bool = False,
) -> dict[str, Any]:
    by_pipeline = pending_by_pipeline or []
    return {
        "pending": {
            "total": pending_total if pending_total is not None else sum(p["pending_count"] for p in by_pipeline),
            "oldest_pending_age_seconds": None,
            "by_cause": {},
            "by_pipeline": by_pipeline,
        },
        "drain": {"ratio": 1.0, "depth_change": 0},
        "binding": binding or {"scope": "none", "limit": None, "in_use": None, "headroom": None, "pending_blocked": 0},
        "active_health": {"stall_suspected": stall},
        "series": series or [],
    }


def _pipeline(pipeline_id: uuid.UUID, *, count: int, oldest: float | None = 0.0, name: str = "p") -> dict[str, Any]:
    return {
        "pipeline_id": str(pipeline_id),
        "pipeline_name": name,
        "pending_count": count,
        "oldest_pending_age_seconds": oldest,
        "max_concurrent_runs": 2,
    }


# ---------------------------------------------------------------------------
# queue_pressure_rung
# ---------------------------------------------------------------------------


def test_rung_clear_when_nothing_queued() -> None:
    rung = ca.queue_pressure_rung(
        depth=0, oldest_pending_age_seconds=None, depth_change=0, oldest_age_change=0.0, stall_suspected=False
    )
    assert rung == ca.CLEAR_RUNG


def test_rung_notice_when_queue_growing_below_warning() -> None:
    rung = ca.queue_pressure_rung(
        depth=1, oldest_pending_age_seconds=60.0, depth_change=1, oldest_age_change=60.0, stall_suspected=False
    )
    assert rung == ca.NOTICE_RUNG


def test_rung_warning_when_oldest_pending_crosses_duration() -> None:
    rung = ca.queue_pressure_rung(
        depth=1,
        oldest_pending_age_seconds=ca.WARNING_OLDEST_PENDING_SECONDS,
        depth_change=0,
        oldest_age_change=0.0,
        stall_suspected=False,
    )
    assert rung == ca.WARNING_RUNG


def test_rung_critical_when_climbing_and_not_draining() -> None:
    rung = ca.queue_pressure_rung(
        depth=ca.CRITICAL_MIN_DEPTH,
        oldest_pending_age_seconds=120.0,
        depth_change=2,
        oldest_age_change=120.0,
        stall_suspected=False,
    )
    assert rung == ca.CRITICAL_RUNG


def test_rung_not_critical_when_climbing_below_min_depth() -> None:
    rung = ca.queue_pressure_rung(
        depth=1, oldest_pending_age_seconds=30.0, depth_change=1, oldest_age_change=30.0, stall_suspected=False
    )
    assert rung == ca.NOTICE_RUNG


def test_rung_critical_on_stall_signature_at_min_depth() -> None:
    rung = ca.queue_pressure_rung(
        depth=ca.CRITICAL_MIN_DEPTH,
        oldest_pending_age_seconds=30.0,
        depth_change=0,
        oldest_age_change=0.0,
        stall_suspected=True,
    )
    assert rung == ca.CRITICAL_RUNG


def test_rung_clear_when_depth_flat_and_head_not_ageing() -> None:
    """A stable queue that is NOT growing is not alert-worthy (verdict 'stable')."""
    rung = ca.queue_pressure_rung(
        depth=5, oldest_pending_age_seconds=30.0, depth_change=0, oldest_age_change=0.0, stall_suspected=False
    )
    assert rung == ca.CLEAR_RUNG


# ---------------------------------------------------------------------------
# saturation_rung
# ---------------------------------------------------------------------------


def _org_binding() -> dict[str, Any]:
    return {"scope": "org_run", "limit": 5, "in_use": 5, "headroom": 0, "pending_blocked": 3}


def test_saturation_clear_when_nothing_binds() -> None:
    report = _report(binding={"scope": "none", "limit": None, "in_use": None, "headroom": None, "pending_blocked": 0})
    rung = ca.saturation_rung(report, bound_since=_NOW - timedelta(hours=1), now=_NOW)
    assert rung == ca.CLEAR_RUNG


def test_saturation_clear_while_under_sustain_threshold() -> None:
    report = _report(binding=_org_binding(), pending_total=3)
    rung = ca.saturation_rung(report, bound_since=_NOW - timedelta(seconds=30), now=_NOW)
    assert rung == ca.CLEAR_RUNG


def test_saturation_fires_after_sustain_threshold_with_work_blocked() -> None:
    report = _report(binding=_org_binding(), pending_total=3)
    rung = ca.saturation_rung(report, bound_since=_NOW - timedelta(seconds=ca.SATURATION_SUSTAIN_SECONDS), now=_NOW)
    assert rung == ca.SATURATION_RUNG


def test_saturation_clear_when_cap_held_but_nothing_queued() -> None:
    """A fully-held cap with an empty queue is a leak/stall, not saturation."""
    binding = {"scope": "org_run", "limit": 5, "in_use": 5, "headroom": 0, "pending_blocked": 0}
    report = _report(binding=binding, pending_total=0)
    rung = ca.saturation_rung(report, bound_since=_NOW - timedelta(hours=1), now=_NOW)
    assert rung == ca.CLEAR_RUNG


def test_saturation_ignores_pipeline_scope_binding() -> None:
    binding = {"scope": "pipeline", "limit": 2, "in_use": 2, "headroom": 0, "pending_blocked": 3}
    report = _report(binding=binding, pending_total=3)
    rung = ca.saturation_rung(report, bound_since=_NOW - timedelta(hours=1), now=_NOW)
    assert rung == ca.CLEAR_RUNG


# ---------------------------------------------------------------------------
# pipeline_trend
# ---------------------------------------------------------------------------


def test_pipeline_trend_reads_growth_from_the_series() -> None:
    pid = uuid.uuid4()
    series = [
        {"by_pipeline": []},
        {"by_pipeline": [{"pipeline_id": str(pid), "pending_depth": 2, "oldest_pending_age_seconds": 10.0}]},
        {"by_pipeline": [{"pipeline_id": str(pid), "pending_depth": 5, "oldest_pending_age_seconds": 130.0}]},
    ]
    depth_change, age_change = ca.pipeline_trend(_report(series=series), pid)
    assert depth_change == 5
    assert age_change == 130.0


def test_pipeline_trend_none_without_two_buckets() -> None:
    depth_change, age_change = ca.pipeline_trend(_report(series=[{"by_pipeline": []}]), uuid.uuid4())
    assert depth_change is None
    assert age_change is None


# ---------------------------------------------------------------------------
# resolve_alert_action — dedup / escalation / auto-clear
# ---------------------------------------------------------------------------


def test_action_notifies_on_first_observation() -> None:
    action, stored = ca.resolve_alert_action(None, ca.NOTICE_RUNG)
    assert action == "notify"
    assert stored == ca.NOTICE_RUNG


def test_action_escalates_to_higher_rung() -> None:
    action, stored = ca.resolve_alert_action(ca.NOTICE_RUNG, ca.WARNING_RUNG)
    assert action == "notify"
    assert stored == ca.WARNING_RUNG


def test_action_holds_at_same_rung() -> None:
    action, stored = ca.resolve_alert_action(ca.WARNING_RUNG, ca.WARNING_RUNG)
    assert action == "hold"
    assert stored == ca.WARNING_RUNG


def test_action_holds_when_rung_falls_without_clearing() -> None:
    """The high-water mark is not lowered on a dip — no re-notify storm."""
    action, stored = ca.resolve_alert_action(ca.CRITICAL_RUNG, ca.NOTICE_RUNG)
    assert action == "hold"
    assert stored == ca.CRITICAL_RUNG


def test_action_recovers_a_cleared_warning() -> None:
    action, stored = ca.resolve_alert_action(ca.WARNING_RUNG, ca.CLEAR_RUNG)
    assert action == "recover"
    assert stored is None


def test_action_silently_retires_a_cleared_notice() -> None:
    action, stored = ca.resolve_alert_action(ca.NOTICE_RUNG, ca.CLEAR_RUNG)
    assert action == "hold"
    assert stored is None


def test_action_holds_on_clear_with_no_open_alert() -> None:
    action, stored = ca.resolve_alert_action(None, ca.CLEAR_RUNG)
    assert action == "hold"
    assert stored is None


# ---------------------------------------------------------------------------
# Payload completeness against the render templates
# ---------------------------------------------------------------------------


def _render(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    return _TITLE_TEMPLATES[event_type].format(**payload), _BODY_TEMPLATES[event_type].format(**payload)


def test_queue_payloads_render_without_unknown_placeholder() -> None:
    pid = uuid.uuid4()
    report = _report(
        pending_by_pipeline=[_pipeline(pid, count=4, oldest=900.0, name="nightly")],
        binding={"scope": "org_run", "limit": 5, "in_use": 5, "headroom": 0, "pending_blocked": 4},
        pending_total=4,
    )
    candidates = ca.build_candidates(report, bound_since=None, now=_NOW)
    queue = next(c for c in candidates.values() if c.subject.startswith("queue:"))
    title, body = _render(queue.event_type or "", queue.payload)
    assert "[unknown]" not in title
    assert "[unknown]" not in body
    assert "nightly" in title


def test_saturation_payload_renders_without_unknown_placeholder() -> None:
    report = _report(binding=_org_binding(), pending_total=3)
    candidates = ca.build_candidates(
        report, bound_since=_NOW - timedelta(seconds=ca.SATURATION_SUSTAIN_SECONDS), now=_NOW
    )
    saturation = candidates[ca._SUBJECT_SATURATION]
    title, body = _render(saturation.event_type or "", saturation.payload)
    assert "[unknown]" not in title
    assert "[unknown]" not in body


def test_backpressure_payload_renders_without_unknown_placeholder() -> None:
    pid = uuid.uuid4()
    report = _report(pending_by_pipeline=[_pipeline(pid, count=9)], pending_total=9)
    payload = ca._backpressure_payload(
        pipeline=report["pending"]["by_pipeline"][0], report=report, reason="queue_depth=9 limit=5"
    )
    title, body = _render("capacity_backpressure", payload)
    assert "[unknown]" not in title
    assert "[unknown]" not in body
    assert "queue depth" in body


def test_recovered_payload_renders_without_unknown_placeholder() -> None:
    payload = ca._recovered_payload(subject_label="nightly", previous_rung=ca.WARNING_RUNG)
    title, body = _render("capacity_recovered", payload)
    assert "[unknown]" not in title
    assert "[unknown]" not in body
    assert "nightly" in title


# ---------------------------------------------------------------------------
# Sweep end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def sweep_env(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeRedis, FakeNotifier, dict[str, Any]]:
    state: dict[str, Any] = {"report": _report()}
    monkeypatch.setattr(ca, "read_capacity_report", AsyncMock(side_effect=lambda **_: state["report"]))
    monkeypatch.setattr(ca, "_add_backpressure_candidates", AsyncMock(return_value=None))
    redis = FakeRedis()
    notifier = FakeNotifier()
    return redis, notifier, state


async def _run(redis: FakeRedis, notifier: FakeNotifier, org_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    return await ca.evaluate_capacity_alerts(
        factory=AsyncMock(),
        redis_client=redis,  # type: ignore[arg-type]
        notifier=notifier,
        now=now,
        org_ids=[org_id],
    )


async def test_sweep_skips_without_redis() -> None:
    summary = await ca.evaluate_capacity_alerts(factory=AsyncMock(), redis_client=None, org_ids=[uuid.uuid4()])
    assert summary["state_unavailable"] == 1
    assert summary["dispatched"] == 0


async def test_sweep_fires_once_then_suppresses_identical_tick(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
) -> None:
    redis, notifier, state = sweep_env
    pid = uuid.uuid4()
    org_id = uuid.uuid4()
    state["report"] = _report(
        pending_by_pipeline=[_pipeline(pid, count=4, oldest=900.0)],
        pending_total=4,
    )

    await _run(redis, notifier, org_id, _NOW)
    first = [e[1] for e in notifier.events]
    assert first == ["queue_pressure_warning"]

    # The same observation one tick later must NOT re-notify.
    await _run(redis, notifier, org_id, _NOW + timedelta(minutes=1))
    assert [e[1] for e in notifier.events] == first


async def test_sweep_escalates_to_critical(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
) -> None:
    redis, notifier, state = sweep_env
    pid = uuid.uuid4()
    org_id = uuid.uuid4()
    series = [
        {"by_pipeline": [{"pipeline_id": str(pid), "pending_depth": 1, "oldest_pending_age_seconds": 10.0}]},
        {"by_pipeline": [{"pipeline_id": str(pid), "pending_depth": 6, "oldest_pending_age_seconds": 700.0}]},
    ]
    state["report"] = _report(
        pending_by_pipeline=[_pipeline(pid, count=1, oldest=30.0)],
        series=series,
        pending_total=1,
    )
    await _run(redis, notifier, org_id, _NOW)

    state["report"] = _report(
        pending_by_pipeline=[_pipeline(pid, count=6, oldest=900.0)],
        series=series,
        pending_total=6,
    )
    await _run(redis, notifier, org_id, _NOW + timedelta(minutes=1))

    assert [e[1] for e in notifier.events] == ["queue_pressure_notice", "queue_pressure_critical"]


async def test_sweep_auto_clears_on_drain(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
) -> None:
    redis, notifier, state = sweep_env
    pid = uuid.uuid4()
    org_id = uuid.uuid4()
    state["report"] = _report(
        pending_by_pipeline=[_pipeline(pid, count=4, oldest=900.0)],
        pending_total=4,
    )
    await _run(redis, notifier, org_id, _NOW)

    # The queue drains: the pipeline disappears from the report entirely.
    state["report"] = _report(pending_total=0)
    await _run(redis, notifier, org_id, _NOW + timedelta(minutes=1))

    assert [e[1] for e in notifier.events] == ["queue_pressure_warning", "capacity_recovered"]


async def test_sweep_backpressure_candidate_notifies(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
) -> None:
    redis, notifier, state = sweep_env
    pid = uuid.uuid4()
    org_id = uuid.uuid4()

    async def _hit(factory: Any, report: dict[str, Any], *, org_id: uuid.UUID, candidates: dict[str, Any]) -> None:
        pipeline = report["pending"]["by_pipeline"][0]
        subject = ca._subject_for(ca._SUBJECT_BACKPRESSURE, pipeline_id=pid)
        candidates[subject] = ca.Candidate(
            subject=subject,
            rung=ca.BACKPRESSURE_RUNG,
            event_type="capacity_backpressure",
            payload=ca._backpressure_payload(pipeline=pipeline, report=report, reason="queue_depth=9 limit=5"),
        )

    ca._add_backpressure_candidates.side_effect = _hit  # type: ignore[attr-defined]
    state["report"] = _report(pending_by_pipeline=[_pipeline(pid, count=9)], pending_total=9)
    await _run(redis, notifier, org_id, _NOW)

    assert [e[1] for e in notifier.events] == ["capacity_backpressure"]
    payload = notifier.events[0][2]
    assert payload["backpressure_reason"] == "queue_depth=9 limit=5"
    assert payload["suggested_action"]


async def test_sweep_saturation_sustains_before_firing(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
) -> None:
    redis, notifier, state = sweep_env
    org_id = uuid.uuid4()
    state["report"] = _report(binding=_org_binding(), pending_total=3)

    # First tick only starts the sustain clock — no alert yet.
    await _run(redis, notifier, org_id, _NOW)
    assert not notifier.events

    # Past the sustain window the alert fires.
    await _run(redis, notifier, org_id, _NOW + timedelta(seconds=ca.SATURATION_SUSTAIN_SECONDS + 1))
    assert [e[1] for e in notifier.events] == ["capacity_saturation"]


async def test_sweep_budget_exhausted_is_reported(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
) -> None:
    redis, notifier, _state = sweep_env
    summary = await ca.evaluate_capacity_alerts(
        factory=AsyncMock(),
        redis_client=redis,  # type: ignore[arg-type]
        notifier=notifier,
        now=_NOW,
        org_ids=[uuid.uuid4()],
        budget_seconds=0.0,
    )
    assert summary["budget_exhausted"] is True
    assert summary["orgs_evaluated"] == 0


async def test_sweep_hard_ceiling_cancels_a_wedged_org(
    sweep_env: tuple[FakeRedis, FakeNotifier, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single org whose report read never returns must not overrun the tick.

    The between-org budget check cannot fire while one org is blocked, so the
    hard ``asyncio.wait_for`` ceiling is what bounds the sweep.
    """
    redis, notifier, _state = sweep_env
    release = asyncio.Event()

    async def _hang(**_: Any) -> dict[str, Any]:
        await release.wait()
        return _report()

    monkeypatch.setattr(ca, "read_capacity_report", _hang)
    monkeypatch.setattr(ca, "_HARD_TIMEOUT_SLACK_SECONDS", 0.05)

    summary = await ca.evaluate_capacity_alerts(
        factory=AsyncMock(),
        redis_client=redis,  # type: ignore[arg-type]
        notifier=notifier,
        now=_NOW,
        org_ids=[uuid.uuid4()],
        budget_seconds=0.05,
    )
    assert summary["budget_exhausted"] is True
    assert summary["org_errors"] == 0
    release.set()
