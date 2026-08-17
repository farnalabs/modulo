"""BDD step definitions: Team CRUD operations.

The shared team CRUD steps (``a team ... exists``, ``I create a team ...``,
``I list teams``, ``I get team ...``, ``I rename team ...``, ``I delete team
by id ...`` and the response Then steps) live in conftest.py — the ancestor
of every BDD module — so each step text is defined exactly once. This module
keeps only the steps specific to team_crud.feature.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_crud.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def ctx():
    return {"teams": {}}


@pytest.fixture
def patches():
    collectors = []
    yield collectors
    for p in reversed(collectors):
        with contextlib.suppress(RuntimeError):
            p.stop()


def _make_mock_team(**overrides: Any) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = overrides.get("organisation_id", ORG_ID)
    t.name = overrides.get("name", "test-team")
    t.description = overrides.get("description")
    t.account_id = overrides.get("account_id", uuid.uuid4())
    t.created_at = overrides.get("created_at", _NOW)
    t.updated_at = _NOW
    return t


@given(parsers.parse('a team "{team_name}" already exists'))
def team_already_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = _make_mock_team(name=team_name)


@when(parsers.parse('I delete team "{team_name}"'))
def delete_team(team_name: str, request, ctx, client=None) -> None:
    from unittest.mock import AsyncMock, patch

    from tests.bdd.conftest import _active_client, _store_response

    team = ctx.get("teams", {}).get(team_name)
    if team is None:
        with patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=False):
            resp = _active_client(request, client).delete(f"/api/v1/teams/{uuid.uuid4()}")
        _store_response(request, ctx, resp)
        return

    with patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=True):
        resp = _active_client(request, client).delete(f"/api/v1/teams/{team.id}")
    _store_response(request, ctx, resp)
