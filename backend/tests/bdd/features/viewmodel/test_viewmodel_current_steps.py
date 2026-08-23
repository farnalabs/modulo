"""BDD step definitions: ViewModel Current — the aggregate /api/v1/viewmodel/current endpoint.

Each scenario drives the real FastAPI route through a TestClient, patching only
the CRUD helpers and RLS primitives it depends on, so the scenarios assert the
actual API contract — status codes and response shape.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from tests.bdd.conftest import ORG_ID, USER_ID, _active_client, _shared_state

from modulo.db.crud.base import PageResult

scenarios("viewmodel_current.feature")

_NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------


def _default_org() -> MagicMock:
    org = MagicMock()
    org.id = ORG_ID
    org.name = "Acme Org"
    org.settings_json = {}
    org.daily_spend_limit = None
    return org


def _default_account() -> MagicMock:
    account = MagicMock()
    account.preferences = {}
    return account


def _default_membership(team_id: uuid.UUID, role: str) -> MagicMock:
    membership = MagicMock()
    membership.team_id = team_id
    membership.role = role
    return membership


def _make_pipeline(name: str) -> MagicMock:
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.name = name
    pipeline.visibility = "org"
    pipeline.owner_team_id = None
    pipeline.created_at = _NOW
    pipeline.rate_limit_config = None
    pipeline.max_duration_seconds = None
    pipeline.archived_at = None
    pipeline.snapshot_count = 0
    return pipeline


def _make_run(pipeline_id: uuid.UUID) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = pipeline_id
    run.status = "complete"
    run.trigger_type = "manual"
    run.created_at = _NOW
    return run


def _make_view(name: str, view_type: str = "run_list") -> MagicMock:
    view = MagicMock()
    view.id = uuid.uuid4()
    view.organisation_id = ORG_ID
    view.name = name
    view.description = None
    view.view_type = view_type
    view.filters = {}
    view.columns = None
    view.sort_by = None
    view.sort_order = "desc"
    view.created_by = USER_ID
    view.account_id = USER_ID
    view.created_at = _NOW
    view.updated_at = _NOW
    return view


def _make_hitl(pipeline_id: uuid.UUID) -> MagicMock:
    hitl = MagicMock()
    hitl.id = uuid.uuid4()
    hitl.run_id = uuid.uuid4()
    hitl.pipeline_id = pipeline_id
    hitl.gate_id = "approval_gate"
    hitl.account_id = uuid.uuid4()
    hitl.expires_at = _NOW
    hitl.required_team_id = None
    return hitl


def _make_feature(name: str, description: str) -> MagicMock:
    flag = MagicMock()
    flag.name = name
    flag.description = description
    flag.tier = "community"
    flag.currently_active = True
    return flag


def _feature_name_for(flag: str) -> str:
    descriptions = {
        "parallel_branches": "Run branching logic in parallel within a pipeline",
        "eval_system": "Built-in eval runner for LLM output quality gates",
    }
    return descriptions.get(flag, flag)


def _get_viewmodel(client: Any, ctx: dict[str, Any], params: dict[str, str] | None = None) -> Any:
    """Run the real endpoint with the other modules mocked and stash the response."""
    org = ctx.get("org", _default_org())
    account = ctx.get("account", _default_account())
    memberships = ctx.get("memberships", [])
    pipelines = ctx.get("pipelines", [])
    runs = ctx.get("runs", [])
    views = ctx.get("views", [])
    current_view = ctx.get("current_view")
    org_side_effect: Exception | None = ctx.get("org_exc")

    feature_flags = ctx.get("feature_flags") or ["parallel_branches", "eval_system"]
    plan_flags = [_make_feature(name, _feature_name_for(name)) for name in feature_flags]

    mock_session = ctx.get("_session")
    if mock_session is not None:
        if ctx.get("pending_hitl"):
            mock_session.execute.return_value.all = MagicMock(return_value=ctx["pending_hitl"])
        if ctx.get("team_lookup_missing"):
            mock_session.execute.return_value.scalar_one_or_none = MagicMock(return_value=None)

    with (
        patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.viewmodel.get_organisation",
            new_callable=AsyncMock,
            return_value=org,
            side_effect=org_side_effect,
        ),
        patch("modulo.api.routes.viewmodel.get_account_by_id", new_callable=AsyncMock, return_value=account),
        patch(
            "modulo.api.routes.viewmodel.list_team_memberships_for_account",
            new_callable=AsyncMock,
            return_value=memberships,
        ),
        patch(
            "modulo.api.routes.viewmodel.list_pipelines",
            new_callable=AsyncMock,
            return_value=PageResult(items=pipelines, total=len(pipelines), page=1, page_size=20),
        ),
        patch(
            "modulo.api.routes.viewmodel.list_runs",
            new_callable=AsyncMock,
            return_value=PageResult(items=runs, total=len(runs), page=1, page_size=10),
        ),
        patch(
            "modulo.api.routes.viewmodel.list_views",
            new_callable=AsyncMock,
            return_value=PageResult(items=views, total=len(views), page=1, page_size=100),
        ),
        patch("modulo.api.routes.viewmodel.get_view", new_callable=AsyncMock, return_value=current_view),
        patch(
            "modulo.api.routes.viewmodel.resolve_plan_context",
            new_callable=AsyncMock,
            return_value=MagicMock(list_enabled_features=MagicMock(return_value=plan_flags)),
        ),
        patch("modulo.api.routes.viewmodel._resolve_tier", return_value="team"),
    ):
        return client.get("/api/v1/viewmodel/current", params=params or {})


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('the organisation "{org_name}" is named "{display_name}" with daily spend limit {limit:g}'))
def _given_org_with_limit(org_name: str, display_name: str, limit: float, ctx) -> None:
    org = _default_org()
    org.name = display_name
    org.daily_spend_limit = limit
    ctx["org"] = org


@given(parsers.parse("the account has preferences {preferences_json}"))
def _given_account_preferences(preferences_json: str, ctx) -> None:
    account = _default_account()
    account.preferences = json.loads(preferences_json)
    ctx["account"] = account


@given(parsers.parse('I hold a workspace membership with role "{role}"'))
def _given_membership_role(role: str, ctx) -> None:
    ctx["memberships"] = [_default_membership(team_id=uuid.uuid4(), role=role)]


@given(parsers.parse('the plan enables the features "{first}" and "{second}"'))
def _given_plan_features(first: str, second: str, ctx) -> None:
    ctx["feature_flags"] = [first, second]


@given(parsers.parse('pipeline "{name}" is visible in the org'))
def _given_pipeline_visible(name: str, ctx) -> None:
    pipeline = _make_pipeline(name)
    ctx.setdefault("pipelines", []).append(pipeline)
    ctx["_pipeline_id"] = pipeline.id


@given(parsers.parse('a recent run for "{name}" exists'))
def _given_recent_run(name: str, ctx) -> None:
    pipeline_id = ctx.get("_pipeline_id")
    if pipeline_id is None:
        pipeline = _make_pipeline(name)
        ctx.setdefault("pipelines", []).append(pipeline)
        pipeline_id = pipeline.id
    ctx["runs"] = [_make_run(pipeline_id)]


@given(parsers.parse('a pending approval gate exists for "{name}"'))
def _given_pending_hitl(name: str, ctx, mock_session) -> None:
    pipeline_id = ctx.get("_pipeline_id") or (ctx["pipelines"][0].id if ctx.get("pipelines") else None)
    if pipeline_id is None:
        pipeline = _make_pipeline(name)
        ctx.setdefault("pipelines", []).append(pipeline)
        pipeline_id = pipeline.id
    ctx["pending_hitl"] = [(_make_hitl(pipeline_id), None)]
    ctx["_session"] = mock_session


@given(parsers.parse('a saved view "{name}" of type "{view_type}" exists'))
def _given_saved_view(name: str, view_type: str, ctx) -> None:
    view = _make_view(name, view_type)
    ctx["views"] = [view]
    ctx["view"] = view


@given(parsers.parse('I select the saved view "{name}"'))
def _given_select_view(name: str, ctx) -> None:
    view = ctx.get("view") or _make_view(name)
    ctx["current_view"] = view


@given("the organisation does not exist")
def _given_org_missing(ctx) -> None:
    ctx["org"] = None


@given("the account does not exist")
def _given_account_missing(ctx) -> None:
    ctx["account"] = None


@given("the organisation lookup fails with a programming error")
def _given_org_programming_error(ctx) -> None:
    ctx["org_exc"] = ProgrammingError("SELECT * FROM organisations", {}, Exception("no such table"))


@given("the organisation lookup fails with a database error")
def _given_org_db_error(ctx) -> None:
    ctx["org_exc"] = SQLAlchemyError("connection lost")


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("I GET /api/v1/viewmodel/current")
def _when_get_viewmodel_current(request, ctx, mock_session) -> None:
    ctx.setdefault("_session", mock_session)
    request.node._resp = _get_viewmodel(_active_client(request), ctx)


@when("I GET /api/v1/viewmodel/current without authentication")
def _when_get_viewmodel_current_unauth(request, unauth_client) -> None:
    from modulo.auth.dependencies import get_current_user

    unauth_client.app.dependency_overrides.pop(get_current_user, None)
    request.node._resp = unauth_client.get("/api/v1/viewmodel/current")


@when("I GET /api/v1/viewmodel/current with view_as_team")
def _when_get_viewmodel_view_as_team(request, ctx, mock_session) -> None:
    role = _shared_state(request).get("org_role", "admin")
    client = _active_client(request)
    if role != "admin":
        request.node._resp = client.get("/api/v1/viewmodel/current", params={"view_as_team": str(uuid.uuid4())})
        return
    ctx.setdefault("_session", mock_session)
    request.node._resp = _get_viewmodel(client, ctx, {"view_as_team": str(uuid.uuid4())})


@when("I GET /api/v1/viewmodel/current with unknown view_as_team")
def _when_get_viewmodel_unknown_team(request, ctx, mock_session) -> None:
    ctx["team_lookup_missing"] = True
    ctx.setdefault("_session", mock_session)
    request.node._resp = _get_viewmodel(_active_client(request), ctx, {"view_as_team": str(uuid.uuid4())})


@when("I GET /api/v1/viewmodel/current with the selected view")
def _when_get_viewmodel_selected_view(request, ctx, mock_session) -> None:
    ctx.setdefault("_session", mock_session)
    view = ctx.get("view") or _make_view("Deployments")
    request.node._resp = _get_viewmodel(_active_client(request), ctx, {"current_view_id": str(view.id)})


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('the current user is "{username}" with org role "{org_role}"'))
def _then_current_user(username: str, org_role: str, request) -> None:
    body = request.node._resp.json()
    assert body["user"]["username"] == username, f"Expected username {username!r}, got {body['user']['username']!r}"
    assert body["org_role"] == org_role, f"Expected org_role {org_role!r}, got {body['org_role']!r}"


@then(parsers.parse('the current org is named "{display_name}"'))
def _then_current_org(display_name: str, request) -> None:
    body = request.node._resp.json()
    actual = body["org"]["org_name"]
    assert actual == display_name, f"Expected org name {display_name!r}, got {actual!r}"


@then(parsers.parse('the response includes a team membership with role "{role}"'))
def _then_membership_role(role: str, request) -> None:
    memberships = request.node._resp.json()["team_memberships"]
    roles = [m["team_role"] for m in memberships]
    assert role in roles, f"Expected a membership with team_role {role!r}, got {roles}"


@then("the response includes the account preferences")
def _then_preferences(request) -> None:
    body = request.node._resp.json()
    assert body["preferences"] == {"theme": "dark", "notifications": True}, body["preferences"]


@then("the response includes the enabled feature flags")
def _then_feature_flags(request) -> None:
    body = request.node._resp.json()
    flags = body["feature_flags"]
    names = [f["name"] for f in flags]
    assert "parallel_branches" in names, f"Expected parallel_branches flag, got {names}"
    assert "eval_system" in names, f"Expected eval_system flag, got {names}"
    for flag in flags:
        assert flag["active"] is True
        assert "tier" in flag


@then(parsers.parse('the plan tier is "{tier}" with a daily spend limit of {limit:g}'))
def _then_plan(tier: str, limit: float, request) -> None:
    body = request.node._resp.json()
    assert body["plan"]["tier"] == tier, f"Expected plan tier {tier!r}, got {body['plan']['tier']!r}"
    assert body["plan"]["daily_spend_limit"] == limit, body["plan"]


@then(parsers.re(r'the response lists pipeline "(?P<name>[^"]+)" with (?P<count>\d+) recent runs?'))
def _then_pipelines_and_runs(name: str, count: str, request) -> None:
    count = int(count)
    body = request.node._resp.json()
    pipeline_names = [p["name"] for p in body["pipelines"]]
    assert name in pipeline_names, f"Expected pipeline {name!r} in {pipeline_names}"
    assert body["pipelines_total"] == len(pipeline_names)
    assert len(body["recent_runs"]) == count
    assert body["runs_total"] == count


@then(parsers.re(r"the response includes (?P<count>\d+) pending approval gates?"))
def _then_pending_hitl(count: str, request) -> None:
    count = int(count)
    body = request.node._resp.json()
    gates = body["pending_hitl_gates"]
    assert len(gates) == count, f"Expected {count} pending gate(s), got {len(gates)}"
    if gates:
        assert gates[0]["gate_id"] == "approval_gate"


@then(parsers.parse('the response includes a saved view named "{name}"'))
def _then_saved_views(name: str, request) -> None:
    body = request.node._resp.json()
    assert body["views"] is not None, "Response 'views' is null"
    names = [v["name"] for v in body["views"]]
    assert name in names, f"Expected saved view {name!r} in {names}"


@then(parsers.parse('the response includes the selected current view named "{name}"'))
def _then_current_view(name: str, request) -> None:
    body = request.node._resp.json()
    current = body["current_view"]
    assert current is not None, "Response 'current_view' is null"
    assert current["name"] == name, f"Expected current view {name!r}, got {current['name']!r}"
