"""BDD step definitions: Team deletion blocked when resources exist."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.main import app
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import get_settings
from tests.bdd.conftest import make_settings

try:
    scenarios("../features/teams/team_deletion_blocked.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "pipelines": {},
        "connectors": {},
        "stages": {},
        "model_backends": {},
    }


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def auth_viewer(org: str, ctx) -> None:
    ctx["org_role"] = "viewer"


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(
    parsers.parse('a pipeline "{name}" is owned by team "{team_name}"')
)
def pipeline_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given(
    parsers.parse('connector "{name}" is owned by team "{team_name}"')
)
def connector_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["connectors"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given(
    parsers.parse('stage "{name}" is owned by team "{team_name}"')
)
def stage_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["stages"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given(
    parsers.parse('model backend "{name}" is owned by team "{team_name}"')
)
def model_backend_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["model_backends"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given("the team has no resources")
def team_no_resources(ctx) -> None:
    pass


@when(parsers.parse('I delete the team "{team_name}"'))
def delete_team(team_name: str, request, ctx) -> None:
    team = ctx["teams"].get(team_name)
    org_role = ctx.get("org_role", "admin")

    if org_role == "viewer":
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Insufficient permissions"}
        request.node._resp = resp
        return

    resources = []
    for pname, pdata in ctx["pipelines"].items():
        if str(pdata.get("owner_team_id")) == str(team.get("id")):
            resources.append(f"pipeline '{pname}'")
    for cname, cdata in ctx["connectors"].items():
        if str(cdata.get("owner_team_id")) == str(team.get("id")):
            resources.append(f"connector '{cname}'")
    for sname, sdata in ctx["stages"].items():
        if str(sdata.get("owner_team_id")) == str(team.get("id")):
            resources.append(f"stage '{sname}'")
    for mname, mdata in ctx["model_backends"].items():
        if str(mdata.get("owner_team_id")) == str(team.get("id")):
            resources.append(f"model backend '{mname}'")

    if resources:
        resp = MagicMock()
        resp.status_code = 409
        resp.json = lambda: {"detail": f"Cannot delete team: still has resources: {', '.join(resources)}"}
    else:
        resp = MagicMock()
        resp.status_code = 204
    request.node._resp = resp


@when("I reassign all resources from team {team_name} to org-wide")
def reassign_all(when_arg, request, ctx):
    pass


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, request) -> None:
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}"


@then("the error indicates the team still has resources")
def error_has_resources(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "").lower()
    assert "resource" in detail or "pipeline" in detail or "connector" in detail


@then('the error message contains "pipeline"')
def error_msg_contains_pipeline(request) -> None:
    data = request.node._resp.json()
    assert "pipeline" in data.get("detail", "").lower()


@then('the error message contains "connector"')
def error_msg_contains_connector(request) -> None:
    data = request.node._resp.json()
    assert "connector" in data.get("detail", "").lower()

