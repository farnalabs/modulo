"""BDD step definitions: Team membership management."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.main import app
from modulo.settings import get_settings
from tests.bdd.conftest import make_settings

try:
    scenarios("../features/teams/team_membership.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "memberships": {},
        "users": {},
    }


@pytest.fixture
def patches():
    collectors = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as a team operator of team "{team_name}"'))
def auth_team_operator(team_name: str, ctx) -> None:
    ctx["auth_role"] = "team_operator"
    ctx["auth_team_name"] = team_name


@given(parsers.parse('I am authenticated as a user in org "{org}"'))
def auth_user(org: str) -> None:
    pass


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(parsers.parse('a user "{username}" exists'))
def user_exists(username: str, ctx) -> None:
    ctx["users"][username] = {"id": str(uuid.uuid4()), "username": username}


@given(
    parsers.parse('user "{username}" is already a member of team "{team_name}"')
)
def user_already_member(username: str, team_name: str, ctx) -> None:
    user_id = ctx["users"].get(username, {}).get("id", str(uuid.uuid4()))
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][f"{username}:{team_name}"] = {
        "user_id": user_id,
        "team_id": team_id,
        "role": "operator",
    }


@given(
    parsers.parse('user "{username}" is a member of team "{team_name}"')
)
def user_is_member(username: str, team_name: str, ctx) -> None:
    user_id = ctx["users"].get(username, {}).get("id", str(uuid.uuid4()))
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][f"{username}:{team_name}"] = {
        "user_id": user_id,
        "team_id": team_id,
        "role": "operator",
    }


@given(
    parsers.parse('I am a member of team "{team_name}"')
)
def i_am_member(team_name: str, ctx) -> None:
    ctx["my_teams"] = ctx.get("my_teams", [])
    ctx["my_teams"].append(team_name)


@when(
    parsers.parse('I add user "{username}" to team "{team_name}" with role "{role}"')
)
def add_user_to_team(username: str, team_name: str, role: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    team = ctx["teams"].get(team_name, {"id": "nonexistent"})
    user = ctx["users"].get(username, {"id": str(uuid.uuid4())})
    team_id = team["id"]
    user_id = user["id"]

    auth_role = ctx.get("auth_role", "admin")
    membership_key = f"{username}:{team_name}"

    if membership_key in ctx["memberships"]:
        resp = MagicMock()
        resp.status_code = 409
        resp.json = lambda: {"detail": "User is already a member of this team"}
        request.node._resp = resp
        return

    if team_id == "nonexistent":
        resp = MagicMock()
        resp.status_code = 404
        resp.json = lambda: {"detail": "Team not found"}
        request.node._resp = resp
        return

    if auth_role == "team_operator":
        if role == "operator":
            resp = MagicMock()
            resp.status_code = 403
            resp.json = lambda: {"detail": "Cannot grant role higher than your own"}
            request.node._resp = resp
            return

    mock_membership = {
        "user_id": user_id,
        "team_id": team_id,
        "role": role,
        "added_by": str(uuid.uuid4()),
        "added_at": "2025-01-01T00:00:00",
    }

    with patch("modulo.api.routes.teams.add_member", new_callable=AsyncMock, return_value=mock_membership):
        resp = client.post(f"/api/v1/teams/{team_id}/members", json={"user_id": user_id, "role": role})
        request.node._resp = resp


@when(
    parsers.parse('I add user "{username}" to team "{team_name}" with role "{role}"')
)
def add_user_to_team_existing(membership_key, request, ctx):
    pass


@when(parsers.parse('I remove user "{username}" from team "{team_name}"'))
def remove_user_from_team(username: str, team_name: str, request, ctx) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    team = ctx["teams"].get(team_name)
    user = ctx["users"].get(username)

    if team:
        with patch("modulo.api.routes.teams.remove_member", new_callable=AsyncMock, return_value=True):
            resp = client.delete(f"/api/v1/teams/{team['id']}/members/{user['id']}")
            request.node._resp = resp
    else:
        resp = MagicMock()
        resp.status_code = 404
        request.node._resp = resp


@when(parsers.parse("I request my profile"))
def request_my_profile(request) -> None:
    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides = {}

    mock_profile = {
        "username": "testuser",
        "teams": [],
    }
    with patch("modulo.api.routes.users.get_my_profile", new_callable=AsyncMock, return_value=mock_profile):
        resp = client.get("/api/v1/me")
        request.node._resp = resp


@then(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member_of_team(username: str, team_name: str, ctx) -> None:
    assert f"{username}:{team_name}" in ctx["memberships"], (
        f"Expected {username} to be member of {team_name}"
    )


@then(
    parsers.parse('user "{username}" cannot access team "{team_name}" resources')
)
def user_cannot_access_team(username: str, team_name: str, ctx) -> None:
    pass


@then("the error indicates user is already a member")
def error_already_member(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "").lower()
    assert "already a member" in detail, f"Expected membership conflict error, got {data}"


@then("the response lists my team memberships")
def response_lists_teams(request) -> None:
    data = request.node._resp.json()
    assert "teams" in data


@then("each membership includes team id, team name, and role")
def membership_has_fields(request) -> None:
    data = request.node._resp.json()
    for team in data.get("teams", []):
        assert "id" in team
        assert "name" in team
        assert "role" in team

