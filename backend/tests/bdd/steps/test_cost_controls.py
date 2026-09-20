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


# ===========================================================================
# Hard spend ceilings (FAR-391) — /ceiling surface + run-finalize enforcement
# ===========================================================================


def _usd_cents(amount: str) -> int:
    """Parse a `$12.34`-style step value to integer cents."""
    return int((Decimal(str(amount).replace(",", "")) * 100).to_integral_value())


def _make_ceiling_org(ctx: dict[str, Any]) -> MagicMock:
    """Build the org row the /ceiling routes read (integer-cents columns)."""
    org = MagicMock()
    org.id = _ORG_ID
    org.max_run_cost_cents = ctx.get("max_run_cost_cents")
    org.spend_ceiling_cents = ctx.get("spend_ceiling_cents")
    org.org_cumulative_spend_cents = ctx.get("org_cumulative_spend_cents", 0)
    return org


def _get_ceiling_route(request: Any, ctx: dict[str, Any], client: Any, *, body: dict | None) -> None:
    org = _make_ceiling_org(ctx)
    with (
        patch("modulo.api.routes.costs.get_organisation", new=AsyncMock(return_value=org)),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        if body is None:
            resp = client.get("/api/v1/admin/costs/ceiling")
        else:
            resp = client.put("/api/v1/admin/costs/ceiling", json=body)
    ctx["ceiling_org"] = org
    _store_response(request, ctx, resp)


@given(
    parsers.parse('org "{org_name}" has cost ceilings with max_run_cost ${mrc} and spend_ceiling ${ceiling}'),
)
def org_has_cost_ceilings(org_name: str, mrc: str, ceiling: str, ctx: dict[str, Any]) -> None:
    ctx["max_run_cost_cents"] = _usd_cents(mrc)
    ctx["spend_ceiling_cents"] = _usd_cents(ceiling)


@given(parsers.parse('org "{org_name}" has consumed ${amount} of its ceiling'))
def org_has_consumed_ceiling(org_name: str, amount: str, ctx: dict[str, Any]) -> None:
    ctx["org_cumulative_spend_cents"] = _usd_cents(amount)


@given(parsers.parse('org "{org_name}" has a spend ceiling of ${ceiling} and has consumed it all'))
def org_ceiling_fully_consumed(org_name: str, ceiling: str, ctx: dict[str, Any]) -> None:
    cents = _usd_cents(ceiling)
    ctx["spend_ceiling_cents"] = cents
    ctx["org_cumulative_spend_cents"] = cents


@given(parsers.parse('org "{org_name}" has a per-run ceiling of ${limit}'))
def org_has_per_run_ceiling(org_name: str, limit: str, ctx: dict[str, Any]) -> None:
    ctx["max_run_cost_cents"] = _usd_cents(limit)
    ctx["org_cumulative_spend_cents"] = 0


@when("I GET /api/v1/admin/costs/ceiling")
def admin_get_ceiling(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _get_ceiling_route(request, ctx, client, body=None)


@when(parsers.parse("I PUT /api/v1/admin/costs/ceiling with spend_ceiling ${ceiling}"))
def admin_put_ceiling(ceiling: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    _get_ceiling_route(request, ctx, client, body={"spend_ceiling": float(ceiling.replace(",", ""))})


@when("I PUT /api/v1/admin/costs/ceiling with spend_ceiling null")
def admin_put_ceiling_null(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _get_ceiling_route(request, ctx, client, body={"spend_ceiling": None})


@when("I PUT /api/v1/admin/costs/ceiling with an invalid negative ceiling")
def admin_put_ceiling_negative(request: Any, ctx: dict[str, Any], client: Any) -> None:
    # The request model rejects a negative value (ge=0) before the handler runs.
    _get_ceiling_route(request, ctx, client, body={"spend_ceiling": -5.0})


@then(parsers.parse("the response contains spend_ceiling of {expected}"))
def response_contains_spend_ceiling(expected: str, request: Any) -> None:
    body = request.node.response.json()
    actual = body.get("spend_ceiling")
    assert actual == float(expected), f"Expected spend_ceiling {expected}, got {actual}"


@then(parsers.parse("the response contains max_run_cost of {expected}"))
def response_contains_max_run_cost(expected: str, request: Any) -> None:
    body = request.node.response.json()
    actual = body.get("max_run_cost")
    assert actual == float(expected), f"Expected max_run_cost {expected}, got {actual}"


@then(parsers.parse("the response contains remaining_budget_usd of {expected}"))
def response_contains_remaining_budget(expected: str, request: Any) -> None:
    body = request.node.response.json()
    actual = body.get("remaining_budget_usd")
    assert actual == float(expected), f"Expected remaining_budget_usd {expected}, got {actual}"


@then("the response spend_ceiling is null")
def response_spend_ceiling_is_null(request: Any) -> None:
    body = request.node.response.json()
    assert body.get("spend_ceiling") is None, f"Expected spend_ceiling None, got {body.get('spend_ceiling')}"


@then(parsers.parse("the response ceiling was stored as {cents:d} cents"))
def response_ceiling_stored_cents(cents: int, ctx: dict[str, Any]) -> None:
    org = ctx.get("ceiling_org")
    assert org is not None, "no ceiling write was driven"
    assert org.spend_ceiling_cents == cents, (
        f"Expected spend_ceiling stored as {cents} cents, got {org.spend_ceiling_cents}"
    )


def _run_ledger_block(cost: str, ctx: dict[str, Any]) -> None:
    """Drive ``finalize._ledger_block`` with a mocked session (the established
    cost-controller BDD pattern: real FAR-391 ceiling gate, mocked DB). The run
    and org rows are stored in ``ctx`` for the ``Then`` assertions.
    """
    import asyncio
    from datetime import date

    from modulo.core.cost_controller.finalize import _ledger_block
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.run import Run

    run = MagicMock(spec=Run)
    run.id = uuid.uuid4()
    run.ledger_written = False
    run.ledger_refused_at = None
    run.status = "complete"
    run.error_code = None
    run.error_detail = None
    run.pipeline_id = uuid.uuid4()
    run.owner_team_id = None

    org = MagicMock(spec=Organisation)
    org.id = _ORG_ID
    org.max_run_cost_cents = ctx.get("max_run_cost_cents")
    org.spend_ceiling_cents = ctx.get("spend_ceiling_cents")
    org.org_cumulative_spend_cents = ctx.get("org_cumulative_spend_cents", 0)

    def _execute(stmt):
        text = str(stmt)
        result = MagicMock()
        if "organisations" in text:
            result.scalar_one_or_none = MagicMock(return_value=org)
            result.scalar_one = MagicMock(return_value=org)
        else:
            result.scalar_one = MagicMock(return_value=run)
            result.scalar_one_or_none = MagicMock(return_value=run)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.flush = AsyncMock()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _ledger_block(
                session,
                run_id=run.id,
                org_id=org.id,
                status="complete",
                total=Decimal(str(cost).replace(",", "")),
                owner_team_id=None,
                run_date=date(2026, 6, 24),
                finalize_fields={},
                session_factory=None,
                claim_token=None,
            )
        )
    finally:
        loop.close()

    ctx["ceiling_run"] = run
    ctx["ceiling_org"] = org


@when(parsers.parse("a run with cost ${cost} is finalized"))
def run_finalized_with_cost(cost: str, ctx: dict[str, Any]) -> None:
    _run_ledger_block(cost, ctx)


@then(parsers.parse('the run terminalizes as "{status}"'))
def run_terminalizes(status: str, ctx: dict[str, Any]) -> None:
    run = ctx.get("ceiling_run")
    assert run is not None, "no ceiling enforcement run was driven"
    assert run.status == status, f"Expected terminal state {status!r}, got {run.status!r}"


@then(parsers.parse('the refusal reason is "{reason}"'))
def refusal_reason_is(reason: str, ctx: dict[str, Any]) -> None:
    run = ctx.get("ceiling_run")
    assert run is not None, "no ceiling enforcement run was driven"
    assert run.error_code == reason, f"Expected refusal reason {reason!r}, got {run.error_code!r}"


@then("the run ledger is accepted")
def run_ledger_accepted(ctx: dict[str, Any]) -> None:
    run = ctx.get("ceiling_run")
    assert run is not None, "no ceiling enforcement run was driven"
    assert run.ledger_refused_at is None, "ledger was refused but should have been accepted"


@then("the org cumulative spend is not incremented")
def org_cumulative_not_incremented(ctx: dict[str, Any]) -> None:
    run = ctx.get("ceiling_run")
    assert run is not None and run.ledger_refused_at is not None, "run ledger must be refused"
    org = ctx.get("ceiling_org")
    before = ctx.get("org_cumulative_spend_cents", 0)
    actual = org.org_cumulative_spend_cents
    assert actual == before, f"Expected org cumulative unchanged at {before} cents, got {actual}"


@then(parsers.parse("the org cumulative spend is incremented by ${amount}"))
def org_cumulative_incremented(amount: str, ctx: dict[str, Any]) -> None:
    org = ctx.get("ceiling_org")
    run = ctx.get("ceiling_run")
    assert run is not None and run.ledger_refused_at is None, "run ledger must be accepted to increment spend"
    expected = ctx.get("org_cumulative_spend_cents", 0) + _usd_cents(amount)
    actual = org.org_cumulative_spend_cents
    assert actual == expected, f"Expected org cumulative {expected} cents, got {actual}"


# ===========================================================================
# Scheduled cost reports (/reports)
# ===========================================================================

_REPORT_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


def _make_report() -> MagicMock:
    from datetime import UTC, datetime

    report = MagicMock()
    report.id = _REPORT_ID
    report.period = "weekly"
    report.group_by = "team"
    report.format = "csv"
    report.recipients = ["ops@example.com"]
    report.schedule_type = "recurring"
    report.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return report


@given('org "{org_name}" has a scheduled weekly report')
def org_has_scheduled_report(org_name: str) -> None:
    pass


@given(parsers.parse('org "{org_name}" has a scheduled weekly report with id "{report_id}"'))
def org_has_scheduled_report_with_id(org_name: str, report_id: str, ctx: dict[str, Any]) -> None:
    ctx["report_id"] = report_id


@when("I POST /api/v1/admin/costs/reports with a weekly team CSV report")
def admin_create_report(request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch("modulo.api.routes.costs.create_scheduled_report", new=AsyncMock(return_value=_make_report())),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/admin/costs/reports",
            json={
                "period": "weekly",
                "group_by": "team",
                "format": "csv",
                "recipients": ["ops@example.com"],
                "schedule_type": "recurring",
            },
        )
        _store_response(request, ctx, resp)


@when("I POST /api/v1/admin/costs/reports without recipients")
def admin_create_report_missing_recipients(request: Any, ctx: dict[str, Any], client: Any) -> None:
    # recipients (min_length=1) and group_by are enforced by the request model
    # before the handler runs — no route patching needed for the 422.
    resp = client.post(
        "/api/v1/admin/costs/reports",
        json={"period": "weekly", "group_by": "team"},
    )
    _store_response(request, ctx, resp)


@when("I GET /api/v1/admin/costs/reports")
def admin_list_reports(request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch("modulo.api.routes.costs.list_scheduled_reports", new=AsyncMock(return_value=[_make_report()])),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/costs/reports")
        _store_response(request, ctx, resp)


@when(parsers.parse("I DELETE /api/v1/admin/costs/reports/{report_id}"))
def admin_delete_report(report_id: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    exists = str(ctx.get("report_id")) == report_id
    with (
        patch("modulo.api.routes.costs.delete_scheduled_report", new=AsyncMock(return_value=exists)),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/admin/costs/reports/{report_id}")
        _store_response(request, ctx, resp)


@then(parsers.parse('the response contains report id and period "{period}"'))
def response_contains_report(period: str, request: Any) -> None:
    body = request.node.response.json()
    assert body.get("id") == str(_REPORT_ID), f"Unexpected report id: {body.get('id')}"
    assert body.get("period") == period, f"Expected period {period!r}, got {body.get('period')!r}"


@then("the response contains one scheduled report")
def response_contains_one_report(request: Any) -> None:
    body = request.node.response.json()
    assert isinstance(body, list) and len(body) == 1, f"Expected 1 scheduled report, got {body!r}"
    assert body[0].get("id") == str(_REPORT_ID)


# ===========================================================================
# Spend anomaly detection (/anomalies)
# ===========================================================================

_ANOMALY_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")


@given(
    parsers.parse('org "{org_name}" has a detected spend anomaly of ${amount} against a ${baseline} baseline'),
)
def org_detected_anomaly(org_name: str, amount: str, baseline: str, ctx: dict[str, Any]) -> None:
    ctx["anomaly_amount"] = float(amount.replace(",", ""))
    ctx["anomaly_baseline"] = float(baseline.replace(",", ""))


def _make_persisted_anomaly() -> MagicMock:
    from datetime import UTC, datetime, timedelta

    anomaly = MagicMock()
    anomaly.id = _ANOMALY_ID
    anomaly.anomaly_date = datetime.now(UTC).date() - timedelta(days=1)
    anomaly.pipeline_id = None
    anomaly.amount = 5.0
    anomaly.baseline = 1.0
    anomaly.percent_above = 400.0
    anomaly.dismissed = False
    return anomaly


@when("I GET /api/v1/admin/costs/anomalies")
def admin_get_anomalies(request: Any, ctx: dict[str, Any], client: Any, mock_session: Any) -> None:
    from datetime import UTC, datetime, timedelta

    base = ctx.get("anomaly_baseline", 1.0)
    spike = ctx.get("anomaly_amount", 5.0)
    today = datetime.now(UTC).date()
    rows = [
        MagicMock(run_date=today - timedelta(days=8 - i), daily_spend=base) for i in range(7)
    ]
    rows.append(MagicMock(run_date=today - timedelta(days=1), daily_spend=spike))
    result = MagicMock()
    result.all.return_value = rows
    with (
        patch.object(mock_session, "execute", new=AsyncMock(return_value=result)),
        patch("modulo.api.routes.costs.list_anomalies", new=AsyncMock(return_value=[])),
        patch("modulo.api.routes.costs.record_or_get_anomaly", new=AsyncMock(return_value=_make_persisted_anomaly())),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/costs/anomalies")
        _store_response(request, ctx, resp)


@when("I dismiss the reported anomaly")
def dismiss_reported_anomaly(request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch("modulo.api.routes.costs.dismiss_anomaly", new=AsyncMock(return_value=True)),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.post(f"/api/v1/admin/costs/anomalies/dismiss/{_ANOMALY_ID}")
        _store_response(request, ctx, resp)


@when(parsers.parse("I POST /api/v1/admin/costs/anomalies/dismiss/{anomaly_id}"))
def admin_dismiss_anomaly_by_id(anomaly_id: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch("modulo.api.routes.costs.dismiss_anomaly", new=AsyncMock(return_value=False)),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.post(f"/api/v1/admin/costs/anomalies/dismiss/{anomaly_id}")
        _store_response(request, ctx, resp)


@then("the response contains one fresh anomaly")
def response_contains_one_anomaly(request: Any) -> None:
    body = request.node.response.json()
    assert isinstance(body, list) and len(body) == 1, f"Expected 1 anomaly, got {body!r}"


@then("the anomaly carries a persisted id")
def anomaly_carries_persisted_id(request: Any) -> None:
    body = request.node.response.json()
    assert body, "expected at least one anomaly"
    assert body[0].get("id") == str(_ANOMALY_ID), f"Unexpected anomaly id: {body[0].get('id')}"
    assert body[0].get("dismissed") is False, "fresh anomaly must not be dismissed"


# ===========================================================================
# Cost components (/api/v1/admin/costs/components)
# ===========================================================================

_COMPONENT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")


def _make_component(**overrides: Any) -> MagicMock:
    from modulo.db.models.cost_component import CostComponentKind

    component = MagicMock()
    component.id = overrides.get("id", _COMPONENT_ID)
    component.name = overrides.get("name", "reported_cost")
    component.display_name = overrides.get("display_name", "Reported Cost")
    component.kind = overrides.get("kind", CostComponentKind.SELF_REPORTED.value)
    component.rate_usd = overrides.get("rate_usd", None)
    component.rate_fallback = overrides.get("rate_fallback", None)
    component.formula = overrides.get("formula", None)
    component.report_key = overrides.get("report_key", "model_cost_usd")
    component.enabled = overrides.get("enabled", True)
    component.sort_order = overrides.get("sort_order", 0)
    component.deleted_at = overrides.get("deleted_at", None)
    return component


@given(parsers.parse('a cost component named "{name}" already exists'))
def cost_component_exists(name: str) -> None:
    pass


@given(parsers.parse('a cost component with id "{component_id}"'))
def cost_component_with_id(component_id: str) -> None:
    pass


@given('org "{org_name}" has cost components configured')
def org_has_cost_components(org_name: str) -> None:
    pass


def _post_component(request: Any, ctx: dict[str, Any], client: Any, payload: dict) -> None:
    with (
        patch(
            "modulo.api.routes.cost_components.create_cost_component",
            new=AsyncMock(return_value=_make_component()),
        ),
        patch("modulo.api.routes.cost_components.append_audit_event", new=AsyncMock()),
        patch("modulo.api.routes.cost_components.set_rls_org"),
    ):
        resp = client.post("/api/v1/admin/costs/components", json=payload)
        _store_response(request, ctx, resp)


@when("I POST /api/v1/admin/costs/components with a reportable self_reported component")
def admin_create_component(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _post_component(
        request,
        ctx,
        client,
        {
            "name": "reported_cost",
            "display_name": "Reported Cost",
            "kind": "self_reported",
            "report_key": "model_cost_usd",
        },
    )


@when(parsers.parse('I POST /api/v1/admin/costs/components named "{name}"'))
def admin_create_component_duplicate(name: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch(
            "modulo.api.routes.cost_components.create_cost_component",
            new=AsyncMock(side_effect=ValueError("duplicate_component")),
        ),
        patch("modulo.api.routes.cost_components.append_audit_event", new=AsyncMock()),
        patch("modulo.api.routes.cost_components.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"name": name, "display_name": "LLM Tokens", "kind": "self_reported", "report_key": "model_cost_usd"},
        )
        _store_response(request, ctx, resp)


@when("I POST /api/v1/admin/costs/components with a self_reported component that has a formula")
def admin_create_component_with_formula(request: Any, ctx: dict[str, Any], client: Any) -> None:
    # The request model's cross-field validator rejects self_reported + formula
    # before the handler runs (CostFormulaError -> 422).
    resp = client.post(
        "/api/v1/admin/costs/components",
        json={
            "name": "bad_sr",
            "display_name": "X",
            "kind": "self_reported",
            "formula": "rate * 2",
            "report_key": "model_cost_usd",
        },
    )
    _store_response(request, ctx, resp)


@when("I GET /api/v1/admin/costs/components")
def admin_list_components(request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch(
            "modulo.api.routes.cost_components.list_cost_components",
            new=AsyncMock(return_value=[_make_component(name="llm_tokens", report_key=None)]),
        ),
        patch("modulo.api.routes.cost_components.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/costs/components")
        _store_response(request, ctx, resp)


@when(parsers.parse("I DELETE /api/v1/admin/costs/components/{component_id}"))
def admin_delete_component(component_id: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    with (
        patch(
            "modulo.api.routes.cost_components.soft_delete_cost_component",
            new=AsyncMock(return_value=_make_component()),
        ),
        patch("modulo.api.routes.cost_components.append_audit_event", new=AsyncMock()),
        patch("modulo.api.routes.cost_components.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/admin/costs/components/{component_id}")
        _store_response(request, ctx, resp)


@then(parsers.parse('the response contains component name "{expected}"'))
def response_contains_component_name(expected: str, request: Any) -> None:
    body = request.node.response.json()
    assert body.get("name") == expected, f"Expected component name {expected!r}, got {body.get('name')!r}"


@then("the response contains the configured components")
def response_contains_configured_components(request: Any) -> None:
    body = request.node.response.json()
    assert isinstance(body, list) and len(body) == 1, f"Expected 1 configured component, got {body!r}"
    assert body[0].get("name") == "llm_tokens"
