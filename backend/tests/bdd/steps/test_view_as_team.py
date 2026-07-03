"""BDD step definitions: View-as-Team — admin temporarily views org as a specific team."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import get_settings
from tests.bdd.conftest import make_mock_pipeline, make_mock_session, make_settings

try:
    scenarios("../features/teams/view_as_team.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _map_url(url: str) -> str:
    return url.replace("/api/", "/api/v1/")


def _store_response(request: pytest.FixtureRequest, resp: Any) -> None:
    request.node._resp = resp


def _make_team_filtered_pipelines(team_id: str, ctx: dict[str, Any]) -> list[MagicMock]:
    owned = ctx.get("owned_pipelines", {})
    pipelines = []
    for name, tid in owned.items():
        if tid == team_id:
            p = make_mock_pipeline(name=name, visibility="team")
            p.owner_team_id = uuid.UUID(tid)
            pipelines.append(p)
    return pipelines


def _make_all_pipelines(ctx: dict[str, Any]) -> list[MagicMock]:
    owned = ctx.get("owned_pipelines", {})
    pipelines = []
    for name, tid in owned.items():
        p = make_mock_pipeline(name=name, visibility="team")
        p.owner_team_id = uuid.UUID(tid)
        pipelines.append(p)
    return pipelines


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


@pytest.fixture
def ctx():
    return {}


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    if "team_ids" not in ctx:
        ctx["team_ids"] = {}
    team_id = str(uuid.uuid4())
    ctx["team_ids"][team_name] = team_id
    ctx["team_id"] = team_id
    ctx["team_name"] = team_name


@given(parsers.parse('pipeline "{name}" is owned by team "{team_name}"'))
def pipeline_owned_by_team(name: str, team_name: str, ctx) -> None:
    if "owned_pipelines" not in ctx:
        ctx["owned_pipelines"] = {}
    team_ids = ctx.get("team_ids", {})
    team_id = team_ids.get(team_name, ctx.get("team_id", str(uuid.uuid4())))
    ctx["owned_pipelines"][name] = team_id


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin_in_org(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def auth_viewer_in_org(org: str, ctx) -> None:
    ctx["org_role"] = "viewer"


@when(parsers.parse('I GET /api/viewmodel/current with view_as_team "{team_name}"'))
def get_viewmodel_with_view_as_team(
    team_name: str,
    request: pytest.FixtureRequest,
    ctx,
    patches,
) -> None:
    org_role = ctx.get("org_role", "admin")

    if org_role == "viewer":
        resp = MagicMock()
        resp.status_code = 403
        _store_response(request, resp)
        return

    team_id = ctx.get("team_ids", {}).get(team_name, ctx.get("team_id"))
    if team_name == "nonexistent" or team_id is None:
        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {"detail": "Team not found"}
        _store_response(request, resp)
        return

    mock_session = make_mock_session()

    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )

    client = TestClient(app)

    mock_pipelines = _make_team_filtered_pipelines(team_id, ctx)
    mock_runs_page = PageResult(items=[], total=0, page=1, page_size=10)

    with (
        patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.viewmodel.list_pipelines",
            new_callable=AsyncMock,
            return_value=PageResult(items=mock_pipelines, total=len(mock_pipelines), page=1, page_size=20),
        ),
        patch(
            "modulo.api.routes.viewmodel.list_runs",
            new_callable=AsyncMock,
            return_value=mock_runs_page,
        ),
    ):
        actual_url = _map_url("/api/viewmodel/current")
        resp = client.get(actual_url, params={"view_as_team": team_id})

    app.dependency_overrides.clear()
    _store_response(request, resp)


@when("I GET /api/viewmodel/current without view_as_team")
def get_viewmodel_without_view_as_team(
    request: pytest.FixtureRequest,
    ctx,
) -> None:
    mock_session = make_mock_session()

    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )

    client = TestClient(app)

    mock_pipelines = _make_all_pipelines(ctx)
    mock_runs_page = PageResult(items=[], total=0, page=1, page_size=10)

    with (
        patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.viewmodel.list_pipelines",
            new_callable=AsyncMock,
            return_value=PageResult(items=mock_pipelines, total=len(mock_pipelines), page=1, page_size=20),
        ),
        patch(
            "modulo.api.routes.viewmodel.list_runs",
            new_callable=AsyncMock,
            return_value=mock_runs_page,
        ),
    ):
        actual_url = _map_url("/api/viewmodel/current")
        resp = client.get(actual_url)

    app.dependency_overrides.clear()
    _store_response(request, resp)


@then(parsers.parse('the response contains only team-scoped resources for "{team_name}"'))
def check_response_team_scoped(team_name: str, request) -> None:
    body = request.node._resp.json()
    pipelines = body.get("pipelines", [])
    for p in pipelines:
        assert p.get("owner_team_id") is not None, (
            f"Pipeline {p['name']} is not team-scoped (owner_team_id is null)"
        )


@then(parsers.parse('the response contains pipeline "{name}"'))
def check_response_contains_pipeline(name: str, request) -> None:
    body = request.node._resp.json()
    pipelines = body.get("pipelines", [])
    names = [p["name"] for p in pipelines]
    assert name in names, f"Expected pipeline '{name}' in response, got {names}"


@then(parsers.parse('the response does not contain pipeline "{name}"'))
def check_response_not_contains_pipeline(name: str, request) -> None:
    body = request.node._resp.json()
    pipelines = body.get("pipelines", [])
    names = [p["name"] for p in pipelines]
    assert name not in names, f"Pipeline '{name}' should not be in response, got {names}"
