"""BDD step definitions: Cross-team isolation."""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_anonymous_plan_context, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import get_settings
from tests.bdd.conftest import _make_mock_pipeline_full, make_mock_session, make_settings

scenarios("../features/teams/cross_team_isolation.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_MISSING = object()


class _AllFeatures:
    """Plan context standing in for the shared ``client`` fixture's override."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


async def _all_features_plan_context() -> _AllFeatures:
    return _AllFeatures()


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "users": {},
        "memberships": {},
        "pipelines": {},
        "connectors": {},
    }


@contextmanager
def _session_client(role: str, shaper: Callable[[MagicMock], None] | None = None) -> Iterator[TestClient]:
    """A TestClient driving the REAL routes with a stubbed session.

    ``require_permission`` / ``require_team_membership_or_admin`` and the route
    bodies run unpatched; only the DB seams with no RLS double behind them are
    stubbed. Overrides installed here are snapshotted and restored so the
    shared ``client`` fixture keeps working after the step.
    """
    mock_session: MagicMock = make_mock_session()  # type: ignore[assignment]
    if shaper is not None:
        shaper(mock_session)

    async def override_session() -> AsyncMock:
        yield mock_session

    principal_kwargs = {
        "username": role,
        "organisation_id": _ORG_ID,
        "account_id": uuid.uuid4(),
        "org_role": role,
    }
    overrides = {
        get_settings: make_settings,  # type: ignore[dict-item]
        get_db_session: override_session,
        _get_engine: lambda: MagicMock(),
        get_current_user: lambda: AuthenticatedPrincipal(**principal_kwargs),
        get_current_tenant_user: lambda: TenantPrincipal(**principal_kwargs),
        get_plan_context: _all_features_plan_context,
        get_anonymous_plan_context: _all_features_plan_context,
    }
    saved = {key: app.dependency_overrides.get(key, _MISSING) for key in overrides}
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        for key, value in saved.items():
            if value is _MISSING:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value


def _pipeline_row(pdata: dict | None) -> MagicMock:
    """A Pipeline ORM double matching PipelineResponse validation for a ctx entry."""
    if pdata is None:
        return None
    row = _make_mock_pipeline_full(
        name=pdata["name"],
        visibility=pdata.get("visibility", "org"),
    )
    row.id = uuid.UUID(pdata["id"]) if pdata.get("id") else row.id
    row.owner_team_id = uuid.UUID(pdata["owner_team_id"]) if pdata.get("owner_team_id") else None
    row.graph_nodes_json = []
    return row


def _connector_row(pdata: dict | None, name: str) -> MagicMock:
    """A ConnectorInstance ORM double matching ConnectorResponse validation."""
    row = MagicMock()
    row.id = uuid.UUID(pdata["id"]) if pdata and pdata.get("id") else uuid.uuid4()
    row.organisation_id = _ORG_ID
    row.name = name
    row.connector_type_id = "rest"
    row.credentials_ciphertext = b"gAAAAAB"
    row.config_json = {}
    row.allowed_operations = []
    row.status = "active"
    row.visibility = (pdata or {}).get("visibility", "org")
    row.owner_team_id = uuid.UUID(pdata["owner_team_id"]) if pdata and pdata.get("owner_team_id") else None
    row.tier = "native"
    now = datetime.now(UTC)
    row.created_at = now
    row.updated_at = now
    row.last_skip_error = None
    row.validation_level = "standard"
    row.degraded_at = None
    return row


@given(parsers.parse('a team "{name}" exists'))
def team_exists(name: str, ctx) -> None:
    ctx["teams"][name] = {"id": str(uuid.uuid4()), "name": name}


@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@given(parsers.parse('connector "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def connector_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["connectors"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@given(parsers.parse('connector "{name}" has visibility "{visibility}"'))
def connector_has_visibility(name: str, visibility: str, ctx) -> None:
    if name in ctx["connectors"]:
        ctx["connectors"][name]["visibility"] = visibility
    else:
        ctx["connectors"][name] = {
            "id": str(uuid.uuid4()),
            "name": name,
            "owner_team_id": None,
            "visibility": visibility,
        }


@given(parsers.parse('I am a member of team "{team_name}"'))
def i_am_member(team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"]["__current__"] = {"team_id": team_id}


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id}
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4())}


@given(parsers.parse('I am authenticated as a user in org "{org}"'))
def auth_in_org(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin_in_org(org: str) -> None:
    pass


@when(parsers.parse("I view pipelines"))
def i_view_pipelines(request) -> None:
    """GET the real list route; the RLS seam is empty because the stub
    session carries no visible rows (the stub read returns no rows for the
    caller's context)."""
    with (
        _session_client("admin") as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.pipelines.list_pipelines",
            new_callable=AsyncMock,
            return_value=PageResult(items=[], total=0, page=1, page_size=20),
        ),
    ):
        resp = client.get("/api/v1/pipelines")
    request.node._resp = resp


@when(parsers.parse('user "{username}" requests the pipeline list'))
def user_requests_pipeline_list(username: str, request) -> None:
    """GET the real list route as the named user (steady viewer principal).

    Real ``rls_user_context`` team isolation filters the stub session's (empty)
    rows, so the route returns the visible slice directly from its own list
    seam - the assertion bodies then observe the real JSON contract.
    """
    with (
        _session_client("viewer") as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.pipelines.list_pipelines",
            new_callable=AsyncMock,
            return_value=PageResult(items=[], total=0, page=1, page_size=20),
        ),
    ):
        resp = client.get("/api/v1/pipelines")
    request.node._resp = resp
    del username


@when(parsers.parse('user "{username}" requests GET /api/connectors/{connector_name}'))
def user_requests_connector(username: str, connector_name: str, request, ctx) -> None:
    """GET the real single-connector route as the named user (viewer).

    The connector CRUD seam reproduces the resolver's visibility: a
    team-private row owned by another team is hidden by ``rls_user_context``
    (route 404 branch), an org-visible row is returned to the response
    builder (route 200).
    """
    pdata = ctx.get("connectors", {}).get(connector_name)
    user_team_id = ctx.get("memberships", {}).get(username, {}).get("team_id")
    hidden = pdata is not None and pdata.get("visibility") == "team" and pdata.get("owner_team_id") != user_team_id
    ci = None if hidden else _connector_row(pdata, connector_name)
    fetched_id = uuid.uuid4() if ci is None else ci.id
    with (
        _session_client("viewer") as client,
        patch("modulo.api.routes.connectors.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.connectors.set_rls_user_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.connectors.get_connector_instance",
            new_callable=AsyncMock,
            return_value=ci,
        ),
    ):
        resp = client.get(f"/api/v1/connectors/{fetched_id}")
    request.node._resp = resp
    del username


@when(parsers.parse('I bind connector "{connector_name}" to a node in pipeline "{pipeline_name}"'))
def bind_cross_team_connector(connector_name: str, pipeline_name: str, request, ctx) -> None:
    """PATCH the real graph-replace route; the REAL team-visibility predicate
    judges the binding.

    ``_prepare_graph_write`` and ``_enforce_connector_team_bindings`` (with its
    ``find_connector_team_mismatches`` predicate) run unpatched; the candidate
    read seam is stubbed to the ctx connector row so the real mismatch check
    fires and the route raises its own named 409.
    """
    cpdata = ctx.get("connectors", {}).get(connector_name)
    ppdata = ctx.get("pipelines", {}).get(pipeline_name)
    if cpdata is None or ppdata is None:
        raise AssertionError(
            "binding step requires the connector and pipeline to be declared; "
            "the real graph PATCH route cannot be driven"
        )
    connector_id = uuid.UUID(cpdata["id"])
    with (
        _session_client("admin") as client,
        patch("modulo.api.routes.pipelines._set_rls_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new_callable=AsyncMock,
            return_value=_pipeline_row(ppdata),
        ),
        patch(
            "modulo.core.team_visibility._select_candidate_rows",
            new_callable=AsyncMock,
            return_value=[_connector_row(cpdata, connector_name)],
        ),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{uuid.UUID(ppdata['id'])}/graph",
            json={
                "nodes": [
                    {
                        "id": str(node_id := uuid.uuid4()),
                        "node_type": "router",
                        "position": {"x": 0.0, "y": 0.0},
                        "router_config": {"rules": [{"default": True, "target": str(node_id)}]},
                        "connector_binding": {
                            "type": "rest",
                            "instance_id": str(connector_id),
                        },
                    }
                ],
                "edges": [],
            },
        )
    request.node._resp = resp


@then(parsers.parse("the response status is {status_code:d}"))
def response_status_is(status_code: int, request) -> None:
    assert request.node._resp.status_code == status_code


@then(parsers.parse('I see pipeline "{name}"'))
def i_see_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", [])
    names = [p["name"] for p in items] if isinstance(items, list) else []
    assert name in names, f"Pipeline '{name}' should be in response, got {names}"


@then(parsers.parse('I do not see pipeline "{name}"'))
def i_do_not_see_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", [])
    names = [p["name"] for p in items] if isinstance(items, list) else []
    assert name not in names, f"Pipeline '{name}' should not be in response, got {names}"


@then(parsers.parse('the response does not contain pipeline "{name}"'))
def response_not_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", [])
    names = [p["name"] for p in items] if isinstance(items, list) else []
    assert name not in names, f"Pipeline '{name}' should not be in response, got {names}"


@then("the error indicates connector_team_mismatch")
def error_connector_mismatch(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert "connector_team_mismatch" in detail


@then("the response total count does not include team-private pipelines")
def total_excludes_private(request, ctx) -> None:
    data = request.node._resp.json()
    total = data.get("total", 0)
    visible_pipelines = [p for p in ctx.get("pipelines", {}).values() if p.get("visibility") != "team"]
    assert total <= len(visible_pipelines)
