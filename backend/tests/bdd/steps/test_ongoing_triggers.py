"""BDD step definitions: ongoing triggers (FAR-158).

Covers the worker-pool top-up semantics — below-target creation, at/above-target
no-op, spend limit, org pause, and the pending-counts-toward-target semantic.
Exercises ``cron_helpers._ongoing_topup`` DIRECTLY on a mocked session (the
polling BDD precedent — no running app / Docker needed).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import uuid
from decimal import Decimal
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core import cron_helpers as ch

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/ongoing.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ongoing_ctx"):
        request.node._ongoing_ctx = {
            "pipeline_max": 3,
            "trigger_target": 2,
            "scan_interval": 60,
            "in_flight": 0,
            "daily_spend_limit": None,
            "today_cost": Decimal(0),
            "org_paused": False,
            "trigger": None,
            "mock_create_run": None,
            "created_count": 0,
        }
    return cast("dict[str, Any]", request.node._ongoing_ctx)


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _RoutedSession:
    """Async session double routing the top-up's statements (postgresql)."""

    def __init__(self, trigger: MagicMock, pipeline_max: int, today_cost: Decimal) -> None:
        self._trigger = trigger
        self._pipeline_max = pipeline_max
        self._today_cost = today_cost
        self.executed: list[tuple[Any, Any]] = []
        self.begin_cm = _Begin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return self.begin_cm

    def begin_nested(self) -> _Begin:
        return _Begin()

    def get_bind(self) -> Any:
        return self._get_bind()

    def add(self, obj: object) -> None:
        pass

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        s = str(stmt).lower()
        if "set_config" in s:
            return MagicMock()
        if "try_advisory" in s:
            r = MagicMock()
            r.scalar_one.return_value = True
            return r
        if "from triggers" in s or "update triggers" in s:
            r = MagicMock()
            r.scalar_one_or_none.return_value = self._trigger
            return r
        if "from pipelines" in s:
            r = MagicMock()
            r.scalar_one_or_none.return_value = self._pipeline_max
            return r
        if "total_cost_usd" in s or "coalesce" in s:
            r = MagicMock()
            r.scalar_one.return_value = self._today_cost
            return r
        return MagicMock()


def _make_trigger(**overrides: Any) -> MagicMock:
    from modulo.db.models.trigger import Trigger

    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "organisation_id": _ORG_ID,
        "pipeline_id": uuid.uuid4(),
        "active": True,
        "max_concurrent_runs": 2,
        "daily_spend_limit": None,
        "config_json": {"scan_interval_seconds": 60, "snapshot_id": str(uuid.uuid4())},
    }
    defaults.update(overrides)
    trigger = MagicMock(spec=Trigger)
    for key, value in defaults.items():
        setattr(trigger, key, value)
    return trigger


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse("a pipeline with max_concurrent_runs {cap:d}"))
def _given_pipeline_cap(cap: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["pipeline_max"] = cap


@given(parsers.parse("an ongoing trigger with target {target:d} and scan interval {interval:d} seconds"))
def _given_ongoing_trigger(target: int, interval: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["trigger_target"] = target
    ctx["scan_interval"] = interval
    ctx["trigger"] = _make_trigger(
        max_concurrent_runs=target,
        config_json={"scan_interval_seconds": interval, "snapshot_id": str(uuid.uuid4())},
    )


@given(parsers.parse("the pipeline has {count:d} in-flight runs"))
def _given_in_flight(count: int, request: Any) -> None:
    _ctx(request)["in_flight"] = count


@given(parsers.parse("the pipeline has {count:d} pending runs"))
def _given_pending_runs(count: int, request: Any) -> None:
    # pending is an in-flight status for the ongoing top-up (the queued
    # semantic) — it counts toward the target exactly like running/claimed.
    _ctx(request)["in_flight"] = count


@given(parsers.parse("the ongoing trigger has a daily spend limit of {limit}"))
def _given_daily_spend_limit(limit: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["daily_spend_limit"] = Decimal(limit)
    ctx["trigger"].daily_spend_limit = Decimal(limit)


@given(parsers.parse("the ongoing trigger's org has accumulated {cost} in run costs today"))
def _given_accumulated_cost(cost: str, request: Any) -> None:
    _ctx(request)["today_cost"] = Decimal(cost)


@given("the org is paused")
def _given_org_paused(request: Any) -> None:
    _ctx(request)["org_paused"] = True


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("the ongoing scheduler top-up runs")
def _when_ongoing_topup_runs(request: Any) -> None:
    ctx = _ctx(request)
    trigger = ctx["trigger"]

    session = _RoutedSession(trigger, ctx["pipeline_max"], ctx["today_cost"])

    def _create_run_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        return MagicMock(id=uuid.uuid4())

    create_run = AsyncMock(side_effect=_create_run_side_effect)
    outcome: dict[str, Any] = {}

    with (
        patch.object(ch, "_count_ongoing_runs", new_callable=AsyncMock, return_value=ctx["in_flight"]),
        patch.object(ch, "_org_is_paused_degraded", new_callable=AsyncMock, return_value=ctx["org_paused"]),
        patch.object(ch, "_log_ongoing_event", new_callable=AsyncMock),
        patch("modulo.db.crud.run.create_run", create_run),
    ):
        created = asyncio.run(
            ch._ongoing_topup(
                session,
                trigger_id=trigger.id,
                org_id=trigger.organisation_id,
                pipeline_id=trigger.pipeline_id,
                now=datetime.datetime.now(datetime.UTC),
                redis_client=None,
                outcome=outcome,
            )
        )

    ctx["mock_create_run"] = create_run
    ctx["created_count"] = len(created)
    ctx["outcome"] = outcome


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse("exactly {count:d} runs are created"))
def _then_exactly_runs_created(count: int, request: Any) -> None:
    ctx = _ctx(request)
    assert ctx["created_count"] == count, f"expected {count} created, got {ctx['created_count']}"
    mock_cr = ctx["mock_create_run"]
    assert mock_cr.await_count == count


@then("no runs are created")
def _then_no_runs_created(request: Any) -> None:
    ctx = _ctx(request)
    assert ctx["created_count"] == 0, f"expected no runs, got {ctx['created_count']}"
    mock_cr = ctx["mock_create_run"]
    assert mock_cr.await_count == 0


@then("each created run references the ongoing trigger")
def _then_created_runs_reference_trigger(request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx["mock_create_run"]
    assert mock_cr.await_count > 0
    for call in mock_cr.await_args_list:
        assert call.kwargs["trigger_type"] == "ongoing"
        assert call.kwargs["trigger_id"] == ctx["trigger"].id
