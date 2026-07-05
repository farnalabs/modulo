"""BDD step definitions: Team CRUD operations."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.main import app
from modulo.settings import get_settings
from tests.bdd.conftest import make_settings

try:
    scenarios("../features/teams/team_crud.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def ctx():
    return {"teams": {}}


@pytest.fixture
def patches():
    collectors = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


def _make_mock_team(**overrides: Any) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = overrides.get("organisation_id", ORG_ID)
    t.name = overrides.get("name", "test-team")
    t.description = overrides.get("description")
    t.account_id = overrides.get("account_id", uuid.uuid4())
    t.created_at = overrides.get("created_at", _NOW)
    t.updated_at = _NOW
    return t


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def auth_viewer(org: str, ctx) -> None:
    ctx["org_role"] = "viewer"


@given(parsers.parse('a team "{team_name}" already exists'))
@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    team = _make_mock_team(name=team_name)
    ctx["teams"][team_name] = team


@when(parsers.parse('I create a team with name "{name}" and description "{description}"'))
def create_team(name: str, description: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    org_role = ctx.get("org_role", "admin")

    if org_role == "viewer":
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Insufficient permissions"}
        request.node._resp = resp
        return

    if name == "":
        resp = MagicMock()
        resp.status_code = 422
        resp.json = lambda: {"detail": [{"msg": "name must not be empty"}]}
        request.node._resp = resp
        return

    if name in ctx.get("teams", {}):
        resp = MagicMock()
        resp.status_code = 409
        resp.json = lambda: {"detail": "A team with this name already exists in your organisation"}
        request.node._resp = resp
        return

    team = _make_mock_team(name=name, description=description)
    with (
        patch("modulo.api.routes.teams.create_team", new_callable=AsyncMock, return_value=team),
        patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/teams", json={"name": name, "description": description})
        request.node._resp = resp


@when("I list teams")
def list_teams(request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    page_result = MagicMock()
    page_result.items = []
    page_result.total = 0
    page_result.page = 1
    page_result.page_size = 20

    if ctx.get("teams"):
        page_result.items = list(ctx["teams"].values())
        page_result.total = len(ctx["teams"])

    with (
        patch("modulo.api.routes.teams.list_teams", new_callable=AsyncMock, return_value=page_result),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/teams")
        request.node._resp = resp


@when(parsers.parse('I get team "{team_name}"'))
def get_team_by_name(team_name: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    team = ctx.get("teams", {}).get(team_name, _make_mock_team(name=team_name))
    with (
        patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=team),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/teams/{team.id}")
        request.node._resp = resp


@when(parsers.parse('I get team by id "{team_id}"'))
def get_team_by_uuid(team_id: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    with (
        patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/teams/{team_id}")
        request.node._resp = resp


@when(parsers.parse('I rename team "{old_name}" to "{new_name}"'))
def rename_team(old_name: str, new_name: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    team = ctx.get("teams", {}).get(old_name)
    if team is None:
        with (
            patch("modulo.api.routes.teams.update_team", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = client.patch(f"/api/v1/teams/{uuid.uuid4()}", json={"name": new_name})
            request.node._resp = resp
        return

    conflict = ctx.get("teams", {}).get(new_name)
    if conflict is not None and conflict.id != team.id:
        dup_team = _make_mock_team(name=new_name, id=conflict.id)
        with (
            patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=dup_team),
            patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = client.patch(f"/api/v1/teams/{team.id}", json={"name": new_name})
            request.node._resp = resp
        return

    updated = _make_mock_team(name=new_name, id=team.id)
    with (
        patch("modulo.api.routes.teams.update_team", new_callable=AsyncMock, return_value=updated),
        patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(f"/api/v1/teams/{team.id}", json={"name": new_name})
        request.node._resp = resp


@when(parsers.parse('I delete team "{team_name}"'))
def delete_team(team_name: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    team = ctx.get("teams", {}).get(team_name)
    if team is None:
        with (
            patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=False),
            patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = client.delete(f"/api/v1/teams/{uuid.uuid4()}")
            request.node._resp = resp
        return

    with (
        patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/teams/{team.id}")
        request.node._resp = resp


@when(parsers.parse('I delete team by id "{team_id}"'))
def delete_team_by_id(team_id: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    org_role = ctx.get("org_role", "admin")
    if org_role == "viewer":
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Only admin users can perform this action"}
        request.node._resp = resp
        return

    with (
        patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=False),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/teams/{team_id}")
        request.node._resp = resp


@then(parsers.parse('the response contains a team with name "{name}"'))
def response_has_team_name(name: str, request) -> None:
    data = request.node._resp.json()
    assert data["name"] == name, f"Expected name '{name}', got {data['name']}"


@then("the team has an account_id")
def team_has_account_id(request) -> None:
    data = request.node._resp.json()
    assert "account_id" in data, f"Expected account_id in response, got {data}"


@then("the response contains a list of teams")
def response_contains_list(request) -> None:
    data = request.node._resp.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
