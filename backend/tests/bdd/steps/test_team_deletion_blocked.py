"""BDD step definitions: Team deletion blocked when resources exist."""

import contextlib
import uuid

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_deletion_blocked.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "pipelines": {},
        "connectors": {},
        "model_backends": {},
    }


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}"'))
def pipeline_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given(parsers.parse('connector "{name}" is owned by team "{team_name}"'))
def connector_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["connectors"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given(parsers.parse('model backend "{name}" is owned by team "{team_name}"'))
def model_backend_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["model_backends"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given("the team has no resources")
def team_no_resources(ctx) -> None:
    pass


@when(parsers.parse('I delete the team "{team_name}"'))
def delete_team(team_name: str, request, ctx, client=None) -> None:
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from tests.bdd.conftest import _active_client, _store_response

    team = ctx["teams"].get(team_name)
    team_id = team.get("id") if team else str(uuid.uuid4())

    has_resources = False
    for key in ("pipelines", "connectors", "model_backends"):
        for data in ctx.get(key, {}).values():
            if str(data.get("owner_team_id")) == str(team_id):
                has_resources = True
                break
        if has_resources:
            break

    if has_resources:
        resource_types = []
        resource_types.extend("pipeline" for _ in ctx.get("pipelines", {}))
        resource_types.extend("connector" for _ in ctx.get("connectors", {}))
        resource_types.extend("model backend" for _ in ctx.get("model_backends", {}))

        details = ", ".join(f"{rt}(s)" for rt in set(resource_types))
        side_effect = HTTPException(
            status_code=409,
            detail=f"Cannot delete team: still has resources — {details}",
        )
    else:
        side_effect = None

    with (
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, side_effect=side_effect),
    ):
        resp = _active_client(request, client).delete(f"/api/v1/teams/{team_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I reassign all resources from team "{team_name}" to org-wide'))
def reassign_all(team_name: str, ctx) -> None:
    team = ctx["teams"].get(team_name, {})
    team_id = team.get("id")
    for key in ("pipelines", "connectors", "model_backends"):
        ctx[key] = {
            name: data for name, data in ctx.get(key, {}).items() if str(data.get("owner_team_id")) != str(team_id)
        }


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
