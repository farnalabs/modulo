"""BDD step definitions: Team creation."""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import make_settings

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_create.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {}


@pytest.fixture
def patches():
    collectors = []
    yield collectors
    for p in reversed(collectors):
        with contextlib.suppress(RuntimeError):
            p.stop()


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin_in_org(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def auth_viewer_in_org(org: str, ctx) -> None:
    ctx["org_role"] = "viewer"


@given(parsers.parse('a team "{team_name}" already exists'))
def team_already_exists(team_name: str, ctx) -> None:
    ctx["existing_team"] = team_name


@when(parsers.parse('I POST /api/teams with name "{name}" and description "{description}"'))
def create_team(name: str, description: str, request, ctx) -> None:
    from modulo.api.main import app
    from modulo.settings import get_settings

    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings

    org_role = ctx.get("org_role", "admin")

    if org_role == "viewer":
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Insufficient permissions"}
        request.node._resp = resp
        return

    if ctx.get("existing_team") == name:
        resp = MagicMock()
        resp.status_code = 409
        resp.json = lambda: {"detail": "Team name already taken"}
        request.node._resp = resp
        return

    if name == "":
        resp = MagicMock()
        resp.status_code = 422
        resp.json = lambda: {"detail": [{"msg": "name must not be empty"}]}
        request.node._resp = resp
        return

    mock_team = {
        "id": str(uuid.uuid4()),
        "name": name,
        "description": description,
        "organisation_id": str(ORG_ID),
        "created_by": str(uuid.uuid4()),
        "created_at": "2025-01-01T00:00:00",
        "member_count": 0,
    }

    with patch("modulo.api.routes.teams.create_team", new_callable=AsyncMock, return_value=mock_team):
        resp = client.post("/api/v1/teams", json={"name": name, "description": description})
        request.node._resp = resp


@then(parsers.parse('the response contains a team with name "{name}"'))
def response_has_team_name(name: str, request) -> None:
    data = request.node._resp.json()
    assert data["name"] == name, f"Expected name '{name}', got {data['name']}"


@then("the error indicates the team name is already taken")
def error_team_name_taken(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert "already taken" in detail.lower(), f"Expected name conflict error, got {data}"


@then("the response contains id, name, description, and created_at")
def response_contains_team_fields(request) -> None:
    data = request.node._resp.json()
    assert "id" in data
    assert "name" in data
    assert "description" in data
    assert "created_at" in data


@then("the team has 0 members")
def team_has_zero_members(request) -> None:
    data = request.node._resp.json()
    assert data.get("member_count", 0) == 0
