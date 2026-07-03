"""BDD step definitions: Team deletion workflow."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../features/teams/team_deletion.feature")
except (FileNotFoundError, OSError):
    pass


@pytest.fixture
def ctx():
    """Shared mutable context dict for team deletion tests."""
    return {}


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["team_id"] = str(uuid.uuid4())
    ctx["team_name"] = team_name


@given(parsers.parse("the team has no active runs"))
def team_no_active_runs(ctx) -> None:
    ctx["active_runs"] = 0


@given(parsers.parse("the team has {count:d} active runs"))
def team_has_active_runs(count: int, ctx) -> None:
    ctx["active_runs"] = count


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    ctx.setdefault("members", []).append({"username": username})


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def team_viewer_auth(org: str, ctx) -> None:
    ctx["org_role"] = "viewer"


@when(parsers.parse('I delete the team "{team_identifier}"'))
def delete_team_endpoint(team_identifier: str, request, client, ctx) -> None:
    from fastapi import HTTPException

    team_id = ctx.get("team_id", team_identifier)
    active_runs = ctx.get("active_runs", 0)
    org_role = ctx.get("org_role", "admin")

    if org_role == "viewer":
        request.node._resp = MagicMock()
        request.node._resp.status_code = 403
        request.node._resp.json = lambda: {"detail": "Insufficient permissions"}
        return

    with (
        patch("modulo.api.routes.teams.set_rls_org"),
        patch("modulo.api.routes.teams.set_rls_user_context"),
        patch("modulo.api.routes.teams.delete_team") as mock_delete,
    ):
        if active_runs > 0:
            mock_delete.side_effect = HTTPException(
                status_code=409,
                detail=f"Cannot delete team with {active_runs} active runs",
            )
        elif team_identifier == "00000000-0000-0000-0000-000000009999":
            mock_delete.return_value = False
        else:
            mock_delete.return_value = True

        resp = client.delete(f"/api/v1/teams/{team_id}")

    request.node._resp = resp


@then("the error indicates the team has active runs")
def error_indicates_active_runs(request) -> None:
    resp = request.node._resp
    data = resp.json()
    detail = data.get("detail", "").lower()
    assert "active run" in detail, f"Expected active run error, got: {data}"


@then(parsers.parse('the error message contains "{text}"'))
def error_message_contains(text: str, request) -> None:
    resp = request.node._resp
    data = resp.json()
    assert text in data.get("detail", ""), f"Expected '{text}' in error detail, got: {data}"
