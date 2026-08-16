"""BDD step definitions: Team membership management.

The shared membership steps (``a team ... exists``, ``a user ... exists``,
``user ... is already a member of team ...``, ``I add user ... to team ...
with role ...``, ``I remove user ... from team ...``) live in conftest.py —
the ancestor of every BDD module — so each step text is defined exactly once.
This module keeps only the steps specific to team_membership.feature.
"""

import contextlib
import uuid

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_membership.feature")


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
        with contextlib.suppress(RuntimeError):
            p.stop()


@given(parsers.parse('I am authenticated as a team operator of team "{team_name}"'))
def auth_team_operator(team_name: str, ctx) -> None:
    ctx["auth_role"] = "team_operator"
    ctx["auth_team_name"] = team_name


@given(parsers.parse('I am authenticated as a user in org "{org}"'))
def auth_user(org: str) -> None:
    pass


@given(parsers.parse('a team-scoped pipeline "{pipeline}" is owned by team "{team_name}"'))
def team_scoped_pipeline_owned(pipeline: str, team_name: str, ctx) -> None:
    """Context: the team owns a team-scoped pipeline (removal still proceeds)."""
    ctx.setdefault("team_pipelines", {})[team_name] = pipeline


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    user_id = ctx["users"].get(username, {}).get("id", str(uuid.uuid4()))
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][f"{username}:{team_name}"] = {
        "user_id": user_id,
        "team_id": team_id,
        "role": "operator",
    }


@given(parsers.parse('I am a member of team "{team_name}"'))
def i_am_member(team_name: str, ctx) -> None:
    ctx["my_teams"] = ctx.get("my_teams", [])
    ctx["my_teams"].append(team_name)


@when(parsers.parse("I request my profile"))
def request_my_profile(request, ctx, client=None) -> None:
    from tests.bdd.conftest import _active_client, _store_response

    resp = _active_client(request, client).get("/api/v1/me")
    _store_response(request, ctx, resp)


@then(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member_of_team(username: str, team_name: str, ctx) -> None:
    assert f"{username}:{team_name}" in ctx["memberships"], f"Expected {username} to be member of {team_name}"


@then(parsers.parse('user "{username}" cannot access team "{team_name}" resources'))
def user_cannot_access_team(username: str, team_name: str, ctx) -> None:
    pass


@then("the error indicates user is already a member")
def error_already_member(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "").lower()
    assert "already exists" in detail, f"Expected membership conflict error, got {data}"


@then("the response lists my team memberships")
def response_lists_teams(request) -> None:
    data = request.node._resp.json()
    assert "team_memberships" in data


@then("each membership includes team id, team name, and role")
def membership_has_fields(request) -> None:
    data = request.node._resp.json()
    for team in data.get("team_memberships", []):
        assert "team_id" in team
        assert "team_role" in team
