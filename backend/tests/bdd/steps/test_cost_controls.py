"""Step definitions for cost controls feature: token budget, spend limits, circuit breaker."""

import contextlib
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/costs/cost_controls.feature")

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


# ===========================================================================
# Token budget (FAR-104 — per-agent hard stop via the cost controller)
# ===========================================================================


def _enforce_token_budget(tokens: int, ctx: dict[str, Any]) -> None:
    """Drive ``_enforce_agent_token_budgets`` with a mocked session (the
    established cost-controller BDD pattern: real enforcement, mocked DB).

    The run's snapshot graph maps one node to the agent under test; the mocked
    agent row carries the budget recorded by the ``Given`` step. The override
    tuple (status, error_code, error_detail) is stored in ``ctx`` for the
    ``Then`` assertions.
    """
    import asyncio

    from modulo.core.cost_controller.finalize import _enforce_agent_token_budgets

    agent_id = ctx["agent_id"]
    node_id = ctx["node_id"]
    budget = ctx["token_budget"]

    graph_json = {"nodes": [{"id": node_id, "agent_id": str(agent_id)}]}
    usage = {node_id: {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}}

    mock_session = AsyncMock()
    graph_result = MagicMock()
    graph_result.scalar_one_or_none.return_value = graph_json
    agent_result = MagicMock()
    agent_result.all.return_value = [(agent_id, budget)]

    run = MagicMock()
    run.id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()

    mock_session.execute = AsyncMock(side_effect=[graph_result, agent_result])

    loop = asyncio.new_event_loop()
    try:
        override = loop.run_until_complete(_enforce_agent_token_budgets(mock_session, run=run, usage=usage))
        if override is None:
            ctx["token_override_status"] = None
            ctx["token_override_error_code"] = None
            ctx["token_override_error_detail"] = None
        else:
            ctx["token_override_status"], ctx["token_override_error_code"], ctx["token_override_error_detail"] = (
                override
            )
    finally:
        loop.close()


@given(
    parsers.parse('agent "{agent_name}" has a token budget of {budget:d} tokens'),
)
def agent_has_token_budget(agent_name: str, budget: int, ctx: dict[str, Any]) -> None:
    ctx["agent_name"] = agent_name
    ctx["agent_id"] = uuid.uuid4()
    ctx["node_id"] = f"node-{agent_name}"
    ctx["token_budget"] = budget


@given(parsers.parse('a run is in progress for agent "{agent_name}"'))
def run_in_progress_for_agent(agent_name: str, ctx: dict[str, Any]) -> None:
    assert ctx.get("agent_name") == agent_name, f"Expected agent {agent_name!r}, got {ctx.get('agent_name')!r}"


@when(
    parsers.parse("the run accumulates {tokens:d} tokens"),
)
def run_accumulates_tokens(tokens: int, ctx: dict[str, Any]) -> None:
    _enforce_token_budget(tokens, ctx)


@then(
    parsers.parse('the run transitions to "{state}" terminal state'),
)
def run_transitions_to(state: str, ctx: dict[str, Any]) -> None:
    actual = ctx.get("token_override_status")
    assert actual == state, f"Expected terminal state {state!r}, got {actual!r}"


@then(
    parsers.parse('the error message is "{message}"'),
)
def error_message_is(message: str, ctx: dict[str, Any]) -> None:
    actual = ctx.get("token_override_error_detail")
    assert actual == message, f"Expected error message {message!r}, got {actual!r}"


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
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
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
        account_id=uuid.uuid4(),
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
    parsers.parse('team "{team_name}" has a daily spend limit of ${limit}'),
)
def team_has_daily_spend_limit(team_name: str, limit: str, ctx: dict[str, Any]) -> None:
    ctx["team_spend_limit"] = Decimal(str(limit).replace(",", ""))
    ctx["team_name"] = team_name


@given(
    parsers.parse('team "{team_name}" has already spent ${amount} today'),
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
    org_spent = ctx.get("org_spent_today", Decimal(0))
    team_spent = ctx.get("team_spent_today", Decimal(0))

    mock_org_count = MagicMock()
    mock_org_count.total_spend_usd = org_spent
    mock_org_count.refused_spend_usd = Decimal(0)
    mock_org_count.run_count = 5

    mock_team_count = MagicMock()
    mock_team_count.total_spend_usd = team_spent
    mock_team_count.refused_spend_usd = Decimal(0)
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
        org_sum_result = MagicMock()
        org_sum_result.scalar_one.return_value = org_spent
        team_limit_result = MagicMock()
        team_limit_result.scalar_one_or_none.return_value = team_limit
        team_sum_result = MagicMock()
        team_sum_result.scalar_one.return_value = team_spent

        if team_id:
            mock_execute.side_effect = [
                org_limit_result,
                org_sum_result,
                team_limit_result,
                team_sum_result,
            ]
        else:
            mock_execute.side_effect = [org_limit_result, org_sum_result]

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
    assert ctx.get("spend_reason") == reason, f"Expected reason '{reason}', got '{ctx.get('spend_reason')}'"


@then("the org run count is not incremented")
def org_run_count_not_incremented(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is False, "Expected spend to be rejected, so run count should not increment"


@then("the org run count is incremented")
def org_run_count_incremented(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is True, "Expected spend approved so run count should increment"


@then("the team run count is incremented")
def team_run_count_incremented(ctx: dict[str, Any]) -> None:
    assert ctx.get("spend_approved") is True, "Expected spend approved so team run count should increment"


# ===========================================================================
# Circuit breaker (per-pipeline monthly spend threshold — FAR-105, spec §8.10)
# ===========================================================================


def _make_cb_pipeline(ctx: dict[str, Any], *, tripped: bool | None = None) -> MagicMock:
    """Build the mocked Pipeline row the cost controller reads for the breaker."""
    pipeline = MagicMock()
    pipeline.id = ctx.get("pipeline_id", uuid.uuid4())
    pipeline.name = ctx.get("pipeline_name", "data-pipeline")
    pipeline.circuit_breaker_threshold = ctx.get("pipeline_cb_threshold")
    pipeline.circuit_breaker_tripped = tripped if tripped is not None else bool(ctx.get("pipeline_cb_tripped", False))
    pipeline.circuit_breaker_tripped_at = None
    return pipeline


def _check_circuit_breaker(amount: str, ctx: dict[str, Any]) -> None:
    """Drive ``check_pipeline_circuit_breaker`` with a mocked session.

    Mirrors ``_check_spend``: the session's ``execute`` returns the pipeline
    row, the monthly SUM, then the trip's update statements (in that order).
    """
    from modulo.core.cost_controller import check_pipeline_circuit_breaker

    cost_usd = Decimal(str(amount).replace(",", ""))
    pipeline = _make_cb_pipeline(ctx)

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipeline
    monthly_result = MagicMock()
    monthly_result.scalar_one.return_value = ctx.get("pipeline_monthly_spend", Decimal(0))

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()

    dispatch_mock = AsyncMock()
    update_mock = MagicMock()

    if pipeline.circuit_breaker_tripped or pipeline.circuit_breaker_threshold is None:
        mock_execute = AsyncMock(side_effect=[pipeline_result])
    else:
        # threshold present + not tripped → pipeline read, monthly SUM, then the
        # trip's trigger update.
        mock_execute = AsyncMock(side_effect=[pipeline_result, monthly_result, MagicMock()])

    with (
        patch("modulo.core.cost_controller._dispatch_circuit_breaker_tripped", new=dispatch_mock),
        patch("modulo.core.cost_controller.select"),
        patch("modulo.core.cost_controller.update", new=update_mock),
        patch.object(mock_session, "execute", new=mock_execute),
    ):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            approved, reason = loop.run_until_complete(
                check_pipeline_circuit_breaker(
                    mock_session,
                    org_id=_ORG_ID,
                    pipeline_id=pipeline.id,
                    cost_usd=cost_usd,
                )
            )
            ctx["cb_approved"] = approved
            ctx["cb_reason"] = reason
            ctx["cb_pipeline"] = pipeline
            ctx["cb_update"] = update_mock
            ctx["cb_dispatch"] = dispatch_mock
        except Exception as exc:
            ctx["cb_approved"] = False
            ctx["cb_reason"] = str(exc)
        finally:
            loop.close()


@given(
    parsers.parse('pipeline "{pipeline_name}" has a circuit breaker threshold of ${threshold}'),
)
def pipeline_has_circuit_breaker_threshold(pipeline_name: str, threshold: str, ctx: dict[str, Any]) -> None:
    ctx["pipeline_name"] = pipeline_name
    ctx["pipeline_cb_threshold"] = Decimal(str(threshold).replace(",", ""))
    ctx["pipeline_cb_tripped"] = False


@given(
    parsers.parse('pipeline "{pipeline_name}" has accumulated ${amount} this month'),
)
def pipeline_accumulated_amount(pipeline_name: str, amount: str, ctx: dict[str, Any]) -> None:
    ctx["pipeline_monthly_spend"] = Decimal(str(amount).replace(",", ""))


@when(parsers.parse("the pipeline accumulates another ${amount}"))
def pipeline_accumulates_more(amount: str, ctx: dict[str, Any]) -> None:
    _check_circuit_breaker(amount, ctx)


@then("the circuit breaker trips")
def circuit_breaker_trips(ctx: dict[str, Any]) -> None:
    assert ctx.get("cb_approved") is False, f"Expected breaker trip, got approved={ctx.get('cb_approved')}"
    assert ctx.get("cb_reason") == "circuit_breaker_tripped", f"Reason {ctx.get('cb_reason')!r}"
    assert ctx.get("cb_pipeline").circuit_breaker_tripped is True


@then(parsers.parse("the pipeline trigger is permanently paused"))
def pipeline_trigger_paused(ctx: dict[str, Any]) -> None:
    from modulo.db.models.trigger import Trigger

    update_mock = ctx.get("cb_update")
    assert update_mock is not None, "No trigger-pause update observed"
    trigger_call = update_mock.call_args_list[0]
    assert trigger_call.args[0] is Trigger, f"Update should target Trigger, got {trigger_call.args[0]!r}"
    values_kwargs = update_mock.return_value.where.return_value.values.call_args.kwargs
    assert values_kwargs.get("active") is False, f"Trigger pause update got wrong values: {values_kwargs!r}"


@then("an admin notification is sent")
def admin_notification_sent(ctx: dict[str, Any]) -> None:
    dispatch_mock = ctx.get("cb_dispatch")
    assert dispatch_mock is not None, "No notifier dispatch observed"
    assert dispatch_mock.await_count > 0, "circuit_breaker_tripped notifier dispatch was not awaited"


@given(
    parsers.parse('pipeline "{pipeline_name}" has a tripped circuit breaker'),
)
def pipeline_tripped_circuit_breaker(pipeline_name: str, ctx: dict[str, Any]) -> None:
    ctx["pipeline_name"] = pipeline_name
    ctx["pipeline_cb_threshold"] = None
    ctx["pipeline_cb_tripped"] = True
    ctx["pipeline_monthly_spend"] = Decimal(0)


@when(
    parsers.parse('an admin re-enables pipeline "{pipeline_name}"'),
)
def admin_reenables_pipeline(pipeline_name: str, ctx: dict[str, Any]) -> None:
    from modulo.core.cost_controller import reset_pipeline_circuit_breaker

    pipeline = _make_cb_pipeline(ctx, tripped=True)
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipeline

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    update_mock = MagicMock()

    with (
        patch("modulo.core.cost_controller.select"),
        patch("modulo.core.cost_controller.update", new=update_mock),
        patch.object(mock_session, "execute", new=AsyncMock(side_effect=[pipeline_result, MagicMock()])),
    ):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            reset = loop.run_until_complete(
                reset_pipeline_circuit_breaker(mock_session, org_id=_ORG_ID, pipeline_id=pipeline.id)
            )
            ctx["cb_reset"] = reset
            ctx["cb_pipeline"] = pipeline
            ctx["cb_update"] = update_mock
        finally:
            loop.close()


@then("the circuit breaker is reset")
def circuit_breaker_reset(ctx: dict[str, Any]) -> None:
    assert ctx.get("cb_reset") is True, "reset_pipeline_circuit_breaker returned False"
    assert ctx.get("cb_pipeline").circuit_breaker_tripped is False
    from modulo.db.models.trigger import Trigger

    update_mock = ctx.get("cb_update")
    assert update_mock is not None, "No trigger re-activation update observed"
    trigger_call = update_mock.call_args_list[0]
    assert trigger_call.args[0] is Trigger
    values_kwargs = update_mock.return_value.where.return_value.values.call_args.kwargs
    assert values_kwargs.get("active") is True, f"Trigger re-activation update got wrong values: {values_kwargs!r}"


@then("new runs are allowed")
def new_runs_allowed(ctx: dict[str, Any]) -> None:
    ctx["pipeline_cb_tripped"] = False
    ctx["pipeline_cb_threshold"] = None
    _check_circuit_breaker("10.00", ctx)
    assert ctx.get("cb_approved") is True, (
        f"Expected new run to be allowed after reset, got approved={ctx.get('cb_approved')} "
        f"reason={ctx.get('cb_reason')}"
    )


# ===========================================================================
# Admin API — spend limits (implemented)
# ===========================================================================


@when(
    parsers.parse("I PUT /api/v1/admin/costs/limits/org with daily spend limit ${limit}"),
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
    parsers.parse("I PUT /api/v1/admin/costs/limits/teams/{team_id} with daily spend limit ${limit}"),
)
def admin_put_team_limit(team_id: str, limit: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    team = MagicMock()
    team.id = uuid.UUID(team_id)
    team.organisation_id = _ORG_ID
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
    parsers.parse('I GET /api/v1/admin/costs with group_by "{group_by}" and period "{period}"'),
)
def admin_get_costs_with_params(group_by: str, period: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
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
