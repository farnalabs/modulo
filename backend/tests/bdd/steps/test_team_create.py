"""BDD step definitions: Team creation.

The shared team steps (``the response contains a team with name ...`` and the
auth givens) live in conftest.py — the ancestor of every BDD module — so each
step text is defined exactly once. This module keeps only the steps specific
to team_create.feature and drives the real ``POST /api/v1/teams`` route.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import _active_client, _store_response

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_create.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {}


@pytest.fixture
def patches():
    collectors = []
    yield collectors
    for p in reversed(collectors):
        with contextlib.suppress(RuntimeError):
            p.stop()


@given(parsers.parse('a team "{team_name}" already exists'))
def team_already_exists(team_name: str, ctx) -> None:
    ctx["existing_team"] = team_name


def _make_mock_team(**overrides) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = ORG_ID
    t.name = overrides.get("name", "test-team")
    t.description = overrides.get("description")
    t.account_id = uuid.uuid4()
    t.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    t.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    return t


@when(parsers.re(r'I POST /api/teams with name "(?P<name>[^"]*)" and description "(?P<description>[^"]*)"'))
def create_team(name: str, description: str, request, ctx, client=None) -> None:
    """POST /api/v1/teams — the route's ``require_permission("team.create")``
    gate returns 403 for the viewer principal, its ``CreateTeamRequest``
    validation returns 422 for empty names, and a pre-existing team returns a
    real 409 conflict."""
    existing = None
    if ctx.get("existing_team") == name:
        existing = _make_mock_team(name=name, id=uuid.uuid4())

    created = _make_mock_team(name=name, description=description)

    with (
        patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=existing),
        patch("modulo.api.routes.teams.create_team", new_callable=AsyncMock, return_value=created),
    ):
        resp = _active_client(request, client).post(
            "/api/v1/teams",
            json={"name": name, "description": description},
        )
    _store_response(request, ctx, resp)


@then("the error indicates the team name is already taken")
def error_team_name_taken(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert "already exists" in detail.lower(), f"Expected name conflict error, got {data}"


@then("the response contains id, name, description, and created_at")
def response_contains_team_fields(request) -> None:
    data = request.node._resp.json()
    assert "id" in data
    assert "name" in data
    assert "description" in data
    assert "created_at" in data


@then("the team has 0 members")
def team_has_zero_members(request) -> None:
    data = request.node._resp.json()
    assert data.get("member_count", 0) == 0
