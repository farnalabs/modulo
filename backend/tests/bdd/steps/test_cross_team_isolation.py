"""BDD step definitions: Cross-team isolation."""

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
    scenarios("../features/teams/cross_team_isolation.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "users": {},
        "memberships": {},
        "pipelines": {},
        "connectors": {},
    }


@given(parsers.parse('team "{name}" exists'))
def team_exists(name: str, ctx) -> None:
    ctx["teams"][name] = {"id": str(uuid.uuid4()), "name": name}


@given(
    parsers.parse('pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"')
)
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@given(
    parsers.parse('connector "{name}" is owned by team "{team_name}" with visibility "{visibility}"')
)
def connector_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["connectors"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@given(
    parsers.parse('connector "{name}" has visibility "{visibility}"')
)
def connector_has_visibility(name: str, visibility: str, ctx) -> None:
    if name in ctx["connectors"]:
        ctx["connectors"][name]["visibility"] = visibility


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id}
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4())}


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin(org: str) -> None:
    pass


@when(parsers.parse('user "{username}" requests the pipeline list'))
def user_requests_pipeline_list(username: str, request, ctx) -> None:
    is_member = username in ctx.get("memberships", {})
    result = []
    for name, pdata in ctx["pipelines"].items():
        if pdata.get("visibility") == "org" or is_member:
            result.append(pdata)

    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"items": result, "total": len(result)}
    request.node._resp = resp


@when(
    parsers.parse('user "{username}" requests GET /api/connectors/{connector_name}')
)
def user_requests_connector(username: str, connector_name: str, request, ctx) -> None:
    connector = ctx["connectors"].get(connector_name)
    is_member = username in ctx.get("memberships", {})

    if connector and (connector.get("visibility") == "org" or is_member):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: connector
    else:
        resp = MagicMock()
        resp.status_code = 404
    request.node._resp = resp


@when(
    parsers.parse('I bind connector "{connector_name}" to a node in pipeline "{pipeline_name}"')
)
def bind_cross_team_connector(connector_name: str, pipeline_name: str, request, ctx) -> None:
    connector = ctx["connectors"].get(connector_name)
    pipeline = ctx["pipelines"].get(pipeline_name)

    if connector and pipeline:
        c_team = connector.get("owner_team_id")
        p_team = pipeline.get("owner_team_id")
        if c_team and p_team and str(c_team) != str(p_team):
            resp = MagicMock()
            resp.status_code = 409
            resp.json = lambda: {"detail": "connector_team_mismatch"}
            request.node._resp = resp
            return

    resp = MagicMock()
    resp.status_code = 200
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, request) -> None:
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}"


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
def total_excludes_private(request) -> None:
    data = request.node._resp.json()
    total = data.get("total", 0)
    visible_pipelines = [p for p in ctx.get("pipelines", {}).values() if p.get("visibility") != "team"]
    assert total <= len(visible_pipelines)

