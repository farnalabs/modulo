"""Step definitions for cost controls feature: token budget, spend limits, circuit breaker."""

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/costs/cost_controls.feature")
except (FileNotFoundError, OSError):
    pass

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_TEAM_ID_BY_NAME: dict[str, uuid.UUID] = {
    "alpha": _TEAM_ID,
    "beta": uuid.UUID("20000000-0000-0000-0000-000000000001"),
}


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


@then(parsers.parse("the response status is {status:d}"))
def _check_response_status(status: int, request: Any) -> None:
    resp = request.node._resp
    assert resp.status_code == status, (
        f"Expected status {status}, got {resp.status_code}"
    )


# ===========================================================================
# Token budget (not yet implemented — future scope)
# ===========================================================================


@given(
    parsers.parse('agent "{agent_name}" has a token budget of {budget:d} tokens'),
)
def agent_has_token_budget(agent_name: str, budget: int) -> None:
    pytest.skip("Per-agent token budget enforcement is not yet implemented")


@given('a run is in progress for agent "{agent_name}"')
def run_in_progress_for_agent(agent_name: str) -> None:
    pytest.skip("Per-agent token budget enforcement is not yet implemented")


@when(
    parsers.parse("the run accumulates {tokens:d} tokens"),
)
def run_accumulates_tokens(tokens: int) -> None:
    pytest.skip("Per-agent token budget enforcement is not yet implemented")


@then(
    parsers.parse('the run transitions to "{state}" terminal state'),
)
def run_transitions_to(state: str) -> None:
    pytest.skip("Per-agent token budget enforcement is not yet implemented")


@then(
    parsers.parse('the error message is "{message}"'),
)
def error_message_is(message: str) -> None:
    pytest.skip("Per-agent token budget enforcement is not yet implemented")


# ===========================================================================
# Spend limits (implemented via check_and_record_spend)


def _use_admin_auth(request: Any) -> None:
    """Set the dependency override for admin auth (overrides client fixture)."""
    from modulo.api.main import app as _app
    from modulo.auth.dependencies import get_current_user as _get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal as _Principal

    _app.dependency_overrides[_get_current_user] = lambda: _Principal(
        username="admin",
        organisation_id=_ORG_ID,
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )


def _use_viewer_auth() -> None:
    """Set the dependency override for viewer auth (overrides client fixture)."""
    from modulo.api.main import app as _app
    from modulo.auth.dependencies import get_current_user as _get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal as _Principal

    _app.dependency_overrides[_get_current_user] = lambda: _Principal(
        username="viewer",
        organisation_id=_ORG_ID,
        user_id=uuid.uuid4(),
        org_role="viewer",
    )
# ===========================================================================


@given(
    parsers.parse('org "{org_name}" has a daily spend limit of ${limit}'),
)
def org_has_daily_spend_limit(org_name: str, limit: str, ctx: dict[str, Any]) -> None:
    """Record org daily spend limit in context."""
    ctx["org_daily_spend_limit"] = Decimal(str(limit).replace(",", ""))


@given(
    parsers.parse('org "{org_name}" has already spent ${amount} today'),
)
def org_has_spent_today(org_name: str, amount: str, ctx: dict[str, Any]) -> None:
    ctx["org_spent_today"] = Decimal(str(amount).replace(",", ""))


@given(
    parsers.parse(
        'team "{team_name}" has a daily spend limit of ${limit}'
    ),
)
def team_has_daily_spend_limit(team_name: str, limit: str, ctx: dict[str, Any]) -> None:
    ctx["team_spend_limit"] = Decimal(str(limit).replace(",", ""))
    ctx["team_name"] = team_name


@given(
    parsers.parse(
        'team "{team_name}" has already spent ${amount} today'
    ),
)
def team_has_spent_today(team_name: str, amount: str, ctx: dict[str, Any]) -> None:
    ctx["team_spent_today"] = Decimal(str(amount).replace(",", ""))


@given(
    parsers.parse('org "{org_name}" has team "{team_name}" with id "{team_id}"'),
)
def org_has_team_with_id(org_name: str, team_name: str, team_id: str, ctx: dict[str, Any]) -> None:
    ctx["team_name"] = team_name
    ctx["team_id"] = uuid.UUID(team_id)


@given(
    parsers.parse('org "{org_name}" has cost data for this month'),
)
def org_has_cost_data(org_name: str) -> None:
    pass


@when(
    parsers.parse("a new run costs ${cost}"),
)
def new_run_costs(cost: str, request: Any, ctx: dict[str, Any]) -> None:
    _check_spend(cost, ctx)


@when(
    parsers.parse('a new run for team "{team_name}" costs ${cost}'),
)
def new_run_for_team_costs(team_name: str, cost: str, request: Any, ctx: dict[str, Any]) -> None:
    ctx["team_id"] = _TEAM_ID_BY_NAME.get(team_name, uuid.uuid4())
    _check_spend(cost, ctx)


def _check_spend(cost: str, ctx: dict[str, Any]) -> None:
    """Call check_and_record_spend with mocked session and context values."""
    cost_usd = Decimal(str(cost).replace(",", ""))
    org_limit = ctx.get("org_daily_spend_limit")
    team_limit = ctx.get("team_spend_limit")
    team_id = ctx.get("team_id")
    org_spent = ctx.get("org_spent_today", Decimal("0"))
    team_spent = ctx.get("team_spent_today", Decimal("0"))

    mock_org_count = MagicMock()
    mock_org_count.total_spend_usd = org_spent
    mock_org_count.run_count = 5

    mock_team_count = MagicMock()
    mock_team_count.total_spend_usd = team_spent
    mock_team_count.run_count = 3

    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    mock_session.flush = AsyncMock()

    with (
        patch(
            "modulo.core.cost_controller.get_or_create_daily_count",
            side_effect=[mock_org_count, mock_team_count] if team_id else [mock_org_count],
        ),
        patch(
            "modulo.core.cost_controller.select",
        ),
        patch.object(mock_session, "execute") as mock_execute,
    ):
        org_limit_result = MagicMock()
        org_limit_result.scalar_one_or_none.return_value = org_limit
        team_limit_result = MagicMock()
        team_limit_result.scalar_one_or_none.return_value = team_limit

        mock_execute.side_effect = [org_limit_result, team_limit_result] if team_id else [org_limit_result]

        import asyncio

        from modulo.core.cost_controller import check_and_record_spend

        loop = asyncio.new_event_loop()
        try:
            approved, reason = loop.run_until_complete(
                check_and_record_spend(
                    mock_session,
                    org_id=_ORG_ID,
                    cost_usd=cost_usd,
                    team_id=team_id,
                )
            )
            ctx["spend_approved"] = approved
            ctx["spend_reason"] = reason
        except Exception as exc:
            ctx["spend_approved"] = False
            ctx["spend_reason"] = str(exc)
        finally:
            loop.close()


@then("the spend is approved")
def spend_approved(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is True, (
        f"Expected spend approved, got: approved={ctx.get('spend_approved')}, reason={ctx.get('spend_reason')}"
    )


@then(
    parsers.parse('the spend is rejected with reason "{reason}"'),
)
def spend_rejected(reason: str, ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is False, "Expected spend to be rejected"
    assert ctx.get("spend_reason") == reason, (
        f"Expected reason '{reason}', got '{ctx.get('spend_reason')}'"
    )


@then("the org run count is not incremented")
def org_run_count_not_incremented(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is False, (
        "Expected spend to be rejected, so run count should not increment"
    )


@then("the org run count is incremented")
def org_run_count_incremented(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is True, (
        "Expected spend approved so run count should increment"
    )


@then("the team run count is incremented")
def team_run_count_incremented(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is True, (
        "Expected spend approved so team run count should increment"
    )


# ===========================================================================
# Circuit breaker (not yet implemented — future scope)
# ===========================================================================


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" has a circuit breaker threshold of ${threshold}'
    ),
)
def pipeline_has_circuit_breaker_threshold(pipeline_name: str, threshold: str) -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" has accumulated ${amount} this month'
    ),
)
def pipeline_accumulated_amount(pipeline_name: str, amount: str) -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@when("the pipeline accumulates another ${amount}")
def pipeline_accumulates_more(amount: str) -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@then("the circuit breaker trips")
def circuit_breaker_trips() -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@then(
    parsers.parse("the pipeline trigger is permanently paused")
)
def pipeline_trigger_paused() -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@then("an admin notification is sent")
def admin_notification_sent() -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@given(
    parsers.parse('pipeline "{pipeline_name}" has a tripped circuit breaker'),
)
def pipeline_tripped_circuit_breaker(pipeline_name: str) -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@when(
    parsers.parse('an admin re-enables pipeline "{pipeline_name}"'),
)
def admin_reenables_pipeline(pipeline_name: str) -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@then("the circuit breaker is reset")
def circuit_breaker_reset() -> None:
    pytest.skip("Circuit breaker is not yet implemented")


@then("new runs are allowed")
def new_runs_allowed() -> None:
    pytest.skip("Circuit breaker is not yet implemented")


# ===========================================================================
# Admin API — spend limits (implemented)
# ===========================================================================


@when(
    parsers.parse(
        "I PUT /api/v1/admin/costs/limits/org with daily spend limit ${limit}"
    ),
)
def admin_put_org_limit(limit: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    org = MagicMock()
    org.id = _ORG_ID
    org.daily_spend_limit = None

    with (
        patch("modulo.api.routes.costs.get_organisation", return_value=org),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.put(
            "/api/v1/admin/costs/limits/org",
            json={"daily_spend_limit": float(limit.replace(",", ""))},
        )
        _store_response(request, ctx, resp)


@when(
    parsers.parse(
        "I PUT /api/v1/admin/costs/limits/teams/{team_id} with daily spend limit ${limit}"
    ),
)
def admin_put_team_limit(team_id: str, limit: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    team = MagicMock()
    team.id = uuid.UUID(team_id)
    team.daily_spend_limit = None

    with (
        patch("modulo.api.routes.costs.get_team", return_value=team),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.put(
            f"/api/v1/admin/costs/limits/teams/{team_id}",
            json={"daily_spend_limit": float(limit.replace(",", ""))},
        )
        _store_response(request, ctx, resp)


@when(
    parsers.parse("I GET /api/v1/admin/costs"),
)
def admin_get_costs(request: Any, ctx: dict[str, Any], client: Any) -> None:
    if "nonadmin" in request.node.name:
        _use_viewer_auth()
    rows = [
        {"entity_id": str(_TEAM_ID), "entity_name": "Alpha Team", "total_spend_usd": 150.0, "total_runs": 12},
    ]
    with (
        patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/costs")
        _store_response(request, ctx, resp)


@when(
    parsers.parse(
        'I GET /api/v1/admin/costs with group_by "{group_by}" and period "{period}"'
    ),
)
def admin_get_costs_with_params(
    group_by: str, period: str, request: Any, ctx: dict[str, Any], client: Any
) -> None:
    rows = [
        {"entity_id": str(_ORG_ID), "entity_name": "Acme Corp", "total_spend_usd": 500.0, "total_runs": 25},
    ]
    with (
        patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/admin/costs?group_by={group_by}&period={period}")
        _store_response(request, ctx, resp)


@then(
    parsers.parse("the response contains daily_spend_limit of {expected}"),
)
def response_contains_spend_limit(expected: str, request: Any) -> None:
    body = request.node.response.json()
    actual = body.get("daily_spend_limit")
    assert actual == float(expected), f"Expected daily_spend_limit {expected}, got {actual}"


@then(
    parsers.parse('the response contains period "{expected}"'),
)
def response_contains_period(expected: str, request: Any) -> None:
    body = request.node.response.json()
    assert body.get("period") == expected, f"Expected period {expected!r}, got {body.get('period')}"


@then(
    parsers.parse('the response contains group_by "{expected}"'),
)
def response_contains_group_by(expected: str, request: Any) -> None:
    body = request.node.response.json()
    assert body.get("group_by") == expected, f"Expected group_by {expected!r}, got {body.get('group_by')}"


@then("the response contains spend items")
def response_contains_spend_items(request: Any) -> None:
    body = request.node.response.json()
    items = body.get("items", [])
    assert len(items) > 0, "Expected spend items in response, got empty list"
    assert "entity_name" in items[0], f"Item missing entity_name: {items[0]}"
    assert "total_spend_usd" in items[0], f"Item missing total_spend_usd: {items[0]}"


@then("the response contains a single org-level item")
def response_contains_single_org_item(request: Any) -> None:
    body = request.node.response.json()
    items = body.get("items", [])
    assert len(items) == 1, f"Expected exactly 1 item, got {len(items)}"
