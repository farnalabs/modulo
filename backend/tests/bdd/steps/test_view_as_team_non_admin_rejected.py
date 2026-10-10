"""BDD step definitions: View as team — non-admin rejection."""

import contextlib
import uuid
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import _shaped_execute, _shared_state

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/view_as_team_non_admin_rejected.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {"auth_role": None}


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str) -> None:
    pass


@given(parsers.parse('I am authenticated as an {role} in org "{org}"'))
def auth_as_role(role: str, org: str, ctx) -> None:
    ctx["auth_role"] = role


@given(parsers.parse('I am authenticated as a {role} in org "{org}"'))
def auth_as_role2(role: str, org: str, ctx) -> None:
    ctx["auth_role"] = role


@given(parsers.parse('I authenticate with an API key with role "{role}"'))
def auth_api_key_role(role: str, ctx) -> None:
    ctx["auth_role"] = f"api_key_{role}"


@when(parsers.parse('I GET /api/viewmodel/current with view_as_team "{team_name}"'))
def get_viewmodel_with_view_as_team(team_name: str, request, ctx) -> None:
    """Hit the REAL ``GET /api/v1/viewmodel/current`` route for a non-admin.

    The route's ``view_as_team`` admin gate runs unpatched - a non-admin
    principal reaching it with a team parameter is refused 403 before any
    read. The API-key scenarios reuse the same JWT principal shape (the gate
    itself judges only the org role, which is the decision the route owns).
    """
    state = _shared_state(request)
    shared_role = state.get("org_role", "admin")
    ctx_role = ctx.get("auth_role", "")
    # Conftest shared steps (e.g. 'my role is changed to "operator"') write the
    # effective role to the shared state; fall back to this module's own ctx.
    auth_role = shared_role if shared_role != "admin" or not ctx_role else ctx_role
    auth_role = auth_role.removeprefix("api_key_") if auth_role.startswith("api_key_") else auth_role

    team = state["teams"].get(team_name)
    team_query = str(team.id) if team is not None else str(uuid.uuid4())

    from tests.bdd.conftest import session_client

    with session_client(role=auth_role, shaper=lambda session: _shaped_execute(session, [])) as client:
        resp = client.get(f"/api/v1/viewmodel/current?view_as_team={team_query}")
    request.node._resp = resp


@when(parsers.parse('I GET /api/pipelines with view_as_team "{team_name}"'))
def get_pipelines_with_view_as_team(team_name: str, request, ctx) -> None:
    """Hit the REAL ``GET /api/v1/pipelines`` route with the parameter present.

    The pipelines route does not declare a ``view_as_team`` parameter, so the
    real FastAPI handler silently ignores it - that is exactly what the
    scenario asserts (the parameter must never elevate a non-admin into team
    context on endpoints that do not implement view-as-team).
    """
    ctx_role = ctx.get("auth_role", "")
    auth_role = ctx_role.removeprefix("api_key_") if ctx_role.startswith("api_key_") else ctx_role

    def shaper(session: MagicMock) -> None:
        # The page total is the route's only extra read on top of the row
        # fetch (shape it explicitly: 0 pipelines behind the caller's RLS
        # filter - the empty default emits None, which pydantic rejects).
        _shaped_execute(session, [MagicMock(scalar=MagicMock(return_value=0))])

    from tests.bdd.conftest import session_client

    with session_client(role=auth_role, shaper=shaper) as client:
        resp = client.get(f"/api/v1/pipelines?view_as_team={team_name}")
    request.node._resp = resp


@given("my role is changed to {role}")
def role_changed(role: str, ctx) -> None:
    ctx["auth_role"] = role


@then("the view_as_team parameter is ignored")
def view_as_team_ignored() -> None:
    pass
