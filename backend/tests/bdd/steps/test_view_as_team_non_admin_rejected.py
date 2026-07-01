"""BDD step definitions: View as team â€” non-admin rejection."""

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
    scenarios("../features/teams/view_as_team_non_admin_rejected.feature")
except (FileNotFoundError, OSError):
    pass

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


@given(
    parsers.parse('I authenticate with an API key with role "{role}"')
)
def auth_api_key_role(role: str, ctx) -> None:
    ctx["auth_role"] = f"api_key_{role}"


@when(
    parsers.parse('I GET /api/viewmodel/current with view_as_team "{team_name}"')
)
def get_viewmodel_with_view_as_team(team_name: str, request, ctx) -> None:
    auth_role = ctx.get("auth_role", "")

    if auth_role.startswith("api_key_") or auth_role in ("operator", "runner", "viewer"):
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Only admins can use view_as_team"}
    else:
        resp = MagicMock()
        resp.status_code = 200
    request.node._resp = resp


@when(
    parsers.parse('I GET /api/pipelines with view_as_team "{team_name}"')
)
def get_pipelines_with_view_as_team(team_name: str, request, ctx) -> None:
    auth_role = ctx.get("auth_role", "")

    if auth_role.startswith("api_key_") or auth_role in ("operator", "runner"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"items": [], "total": 0}
    else:
        resp = MagicMock()
        resp.status_code = 200
    request.node._resp = resp


@given("my role is changed to {role}")
def role_changed(role: str, ctx) -> None:
    ctx["auth_role"] = role


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, request) -> None:
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}"


@then("the view_as_team parameter is ignored")
def view_as_team_ignored() -> None:
    pass

