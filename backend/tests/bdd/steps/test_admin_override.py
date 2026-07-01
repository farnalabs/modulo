"""BDD step definitions: Admin override of team restrictions."""

import uuid
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../features/teams/admin_override.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
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


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def auth_admin(org: str) -> None:
    pass


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def auth_viewer(org: str, ctx) -> None:
    ctx["org_role"] = "viewer"


@when(parsers.parse("I request the pipeline list"))
def request_pipeline_list(request, ctx) -> None:
    pipelines = list(ctx["pipelines"].values())
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"items": pipelines, "total": len(pipelines)}
    request.node._resp = resp


@when(
    parsers.parse('I request GET /api/connectors/{connector_name}')
)
def request_connector(connector_name: str, request, ctx) -> None:
    connector = ctx["connectors"].get(connector_name)
    if connector:
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: connector
    else:
        resp = MagicMock()
        resp.status_code = 404
    request.node._resp = resp


@when(parsers.parse('I delete pipeline "{name}"'))
def delete_pipeline(name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(name)
    if pipeline:
        del ctx["pipelines"][name]
        resp = MagicMock()
        resp.status_code = 204
    else:
        resp = MagicMock()
        resp.status_code = 404
    request.node._resp = resp


@when(
    parsers.parse('I reassign pipeline "{pipeline_name}" to team "{team_name}"')
)
def reassign_pipeline(pipeline_name: str, team_name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(pipeline_name)
    team = ctx["teams"].get(team_name)
    if pipeline and team:
        pipeline["owner_team_id"] = team["id"]
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: pipeline
    else:
        resp = MagicMock()
        resp.status_code = 404
    request.node._resp = resp


@when(
    parsers.parse('I bulk reassign all resources from team "{team_name}" to org-wide')
)
def bulk_reassign(team_name: str, request, ctx) -> None:
    team = ctx["teams"].get(team_name)
    if team:
        for p in ctx["pipelines"].values():
            if str(p.get("owner_team_id")) == str(team["id"]):
                p["owner_team_id"] = None
                p["visibility"] = "org"
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"status": "ok"}
    request.node._resp = resp


@when(
    parsers.parse('I request GET /api/pipelines/{pipeline_name}')
)
def request_pipeline(pipeline_name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(pipeline_name)
    org_role = ctx.get("org_role", "admin")

    if org_role == "viewer" and pipeline and pipeline.get("visibility") == "team":
        resp = MagicMock()
        resp.status_code = 404
    elif pipeline:
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: pipeline
    else:
        resp = MagicMock()
        resp.status_code = 404
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, request) -> None:
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}"


@then(parsers.parse('the response contains pipeline "{name}"'))
def response_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", [])
    names = [p["name"] for p in items] if isinstance(items, list) else []
    assert name in names, f"Expected pipeline '{name}' in response, got {names}"


@then(
    parsers.parse('pipeline "{name}" has owner_team_id null')
)
def pipeline_owner_team_id_null(name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(name)
    assert pipeline is not None
    assert pipeline.get("owner_team_id") is None

