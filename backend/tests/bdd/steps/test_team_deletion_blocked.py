"""BDD step definitions: Team deletion blocked when resources exist.

Re-anchored to the REAL ``delete_team_endpoint``: only the DB/RLS seams are
patched, so the endpoint's own ``team_has_resources`` guard runs. The shared
``mock_session`` is wired entity-aware (see ``team_deletion_support``), so each
scenario blocks on ITS OWN resource type and the error detail is the
endpoint's real message.
"""

import contextlib
import uuid
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.steps.team_deletion_support import _configure_delete_session

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_deletion_blocked.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Resource bucket in the scenario context -> the DB table the endpoint counts.
_BUCKET_TABLES = {
    "pipelines": "pipelines",
    "connectors": "connector_instances",
    "model_backends": "model_backends",
    "library_primitives": "library_primitives",
}


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "pipelines": {},
        "connectors": {},
        "model_backends": {},
        "library_primitives": {},
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


@given(parsers.parse('library primitive "{name}" is owned by team "{team_name}"'))
def library_primitive_owned_by_team(name: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["library_primitives"][name] = {"id": str(uuid.uuid4()), "name": name, "owner_team_id": team_id}


@given("the team has no resources")
def team_no_resources(ctx) -> None:
    pass


@when(parsers.parse('I delete the team "{team_name}"'))
def delete_team(team_name: str, request, ctx, client=None) -> None:
    from tests.bdd.conftest import _active_client, _store_response

    mock_session = request.getfixturevalue("mock_session")
    team = ctx["teams"].get(team_name)
    team_id = team.get("id") if team else str(uuid.uuid4())

    # Count only resources actually owned by THIS team, per DB table. Buckets
    # the scenario never seeded simply do not own anything (the entity-aware
    # dispatcher answers their count reads with the zero default).
    table_counts = {
        table: sum(1 for data in ctx.get(bucket, {}).values() if str(data.get("owner_team_id")) == str(team_id))
        for bucket, table in _BUCKET_TABLES.items()
    }
    team_row = MagicMock() if team is not None else None
    _configure_delete_session(mock_session, table_counts=table_counts, team_row=team_row)

    with (
        patch("modulo.api.routes.teams.set_rls_org"),
        patch("modulo.api.routes.teams.set_rls_user_context"),
    ):
        resp = _active_client(request, client).delete(f"/api/v1/teams/{team_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I reassign all resources from team "{team_name}" to org-wide'))
def reassign_all(team_name: str, ctx) -> None:
    team = ctx["teams"].get(team_name, {})
    team_id = team.get("id")
    for key in _BUCKET_TABLES:
        ctx[key] = {
            name: data for name, data in ctx.get(key, {}).items() if str(data.get("owner_team_id")) != str(team_id)
        }


@then("the error indicates the team still has resources")
def error_has_resources(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert "team_has_resources" in detail, f"Expected the resource guard error, got: {data}"
    assert "still has resources" in detail, f"Expected 'still has resources', got: {data}"


@then(parsers.parse('the error message contains "{text}"'))
def error_message_contains(text: str, request) -> None:
    data = request.node._resp.json()
    assert text in data.get("detail", ""), f"Expected '{text}' in error detail, got: {data}"


@then(parsers.parse('the error message does not contain "{text}"'))
def error_message_does_not_contain(text: str, request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert text not in detail, f"Expected '{text}' NOT in error detail, got: {data}"
