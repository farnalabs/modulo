"""BDD step definitions: Quality Report Delivery."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/reports/quality_report.feature")

from tests.bdd.conftest import make_mock_pipeline  # noqa: E402


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_url(url: str) -> str:
    """Translate feature-file URLs (/api/...) to actual API routes (/api/v1/...)."""
    return url.replace("/api/", "/api/v1/")


def _patch_set_rls(patches: list[Any]) -> None:
    for mod_path in (
        "modulo.api.routes.pipelines.set_rls_org",
        "modulo.api.routes.pipelines.set_rls_user_context",
    ):
        patcher = patch(mod_path, new_callable=AsyncMock)
        patcher.start()
        patches.append(patcher)


def _setup_notification_endpoints(session: MagicMock, endpoints: list[MagicMock]) -> None:
    original_return = session.execute.return_value

    async def _execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
        q = args[0] if args else None
        q_str = str(q) if q is not None else ""
        if "notification_endpoints" in q_str.lower():
            iterable_mock = MagicMock()
            iterable_mock.__iter__.side_effect = lambda: iter(endpoints)
            result_mock = AsyncMock()
            result_mock.scalar_one_or_none = AsyncMock(return_value=None)
            result_mock.scalars = MagicMock(return_value=iterable_mock)
            return result_mock
        return original_return

    session.execute = AsyncMock(side_effect=_execute_side_effect)


def _make_notification_endpoint(
    *,
    url: str = "https://hooks.slack.com/services/T1/B1/xxx",
    events: list[str] | None = None,
) -> MagicMock:
    import json
    ep = MagicMock()
    ep.url = url
    ep.events = json.dumps(events or ["quality_report"])
    return ep


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request) -> None:
    request.node._mock_pipeline = make_mock_pipeline(name=name)
    request.node._pipeline_name = name


@given(parsers.parse("the pipeline has a webhook configured for quality_report events"))
def pipeline_has_webhook(request) -> None:
    request.node._has_webhook = True


@given("the pipeline has no notification endpoints")
def pipeline_no_webhook(request) -> None:
    pass


@given("no pipeline exists for quality report")
def no_pipeline_exists(request) -> None:
    pass


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('I POST to the quality report endpoint for "{name}"'))
def trigger_quality_report(name: str, client, request, patches, mock_session: MagicMock) -> None:

    pipeline = getattr(request.node, "_mock_pipeline", None)
    pipeline_id = pipeline.id if pipeline else uuid.uuid4()

    if getattr(request.node, "_has_webhook", False):
        endpoints = [_make_notification_endpoint()]
        _setup_notification_endpoints(mock_session, endpoints)

    _patch_set_rls(patches)
    p = patch(
        "modulo.api.routes.pipelines.get_pipeline",
        new_callable=AsyncMock,
        return_value=pipeline,
    )
    p.start()
    patches.append(p)

    p2 = patch(
        "modulo.api.routes.pipelines.generate_quality_report",
        new_callable=AsyncMock,
        return_value={
            "period": {"start": "2026-06-25", "end": "2026-07-01"},
            "summary": {"total_runs": 100, "avg_eval_pass_rate": 85.0, "total_cost_usd": 50.0},
            "week_over_week": {
                "runs_delta_pct": 10.0,
                "eval_pass_rate_delta_pct": 5.0,
                "cost_delta_pct": -3.0,
                "previous_week_runs": 90,
                "previous_week_avg_pass_rate": 80.0,
                "previous_week_cost_usd": 51.5,
            },
            "trend": [{"date": "2026-07-01", "run_count": 10, "eval_pass_rate": 85.0, "token_spend_usd": 5.0}],
            "eval_breakdown": {
                "current_week": {"total_evals": 50, "passed_evals": 40, "pass_rate": 80.0},
                "previous_week": {"total_evals": 40, "passed_evals": 30, "pass_rate": 75.0},
            },
        },
    )
    p2.start()
    patches.append(p2)

    p3 = patch(
        "modulo.api.routes.pipelines.deliver_quality_report",
        new_callable=AsyncMock,
        return_value=[{"url": "https://hooks.slack.com/xxx", "status": "delivered", "status_code": 200, "error": None}],
    )
    p3.start()
    patches.append(p3)

    url = _map_url(f"/api/pipelines/{pipeline_id}/quality-report")
    resp = client.post(url)
    request.node._resp = resp


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the response contains period, summary, and deliveries")
def check_response_body(request) -> None:
    data = request.node._resp.json()
    assert "period" in data
    assert "summary" in data
    assert "deliveries" in data
    assert len(data["deliveries"]) > 0


@then("the response contains empty deliveries")
def check_empty_deliveries(request) -> None:
    data = request.node._resp.json()
    assert "deliveries" in data
    assert data["deliveries"] == []
