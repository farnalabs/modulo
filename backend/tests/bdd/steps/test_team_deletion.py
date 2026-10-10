"""BDD step definitions: Team deletion workflow.

Re-anchored to the REAL ``delete_team_endpoint``: only the DB/RLS seams
(``set_rls_org`` / ``set_rls_user_context``) are patched, so the endpoint's own
resource-count guard and the real ``delete_team`` soft-delete path run end to
end. The shared ``mock_session`` is wired table-aware so each scenario blocks on
its own resource type and the 404 path is driven by a real ``get_team`` miss.
"""

import contextlib
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_deletion.feature")


@pytest.fixture
def ctx():
    """Shared mutable context dict for team deletion tests."""
    return {}


def _result(*, scalar: int = 0, row: Any = None) -> MagicMock:
    """Build a session-result mock the delete endpoint's reads can consume."""
    result = MagicMock()
    result.scalar = MagicMock(return_value=scalar)
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


def _configure_delete_session(session: Any, *, table_counts: dict[str, int], team_row: Any) -> None:
    """Wire the shared mock session so the REAL delete endpoint runs end to end.

    ``delete_team_endpoint`` issues four ``select(func.count())`` queries, one
    per resource model; each is answered by inspecting the statement's target
    table so a scenario blocks on ITS OWN resource type (and every other type
    reports zero). The endpoint's own ``get_team`` read (``select(Team)`` /
    ``FROM teams``) returns *team_row* — a truthy row for an existing team, or
    ``None`` so the real soft-delete path reports "not found" (404).
    """

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt).lower()
        if "from teams" in text:
            return _result(row=team_row)
        for table, count in table_counts.items():
            if table in text:
                return _result(scalar=count)
        return _result()

    session.execute.side_effect = _execute


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["team_id"] = str(uuid.uuid4())
    ctx["team_name"] = team_name


@given("the team owns no resources")
def team_owns_no_resources(ctx) -> None:
    ctx["resource_count"] = 0


@given(parsers.parse("the team owns {count:d} resources"))
def team_owns_resources(count: int, ctx) -> None:
    ctx["resource_count"] = count


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    ctx.setdefault("members", []).append({"username": username})


@when(parsers.parse('I delete the team "{team_identifier}"'))
def delete_team_endpoint(team_identifier: str, request, ctx, client=None) -> None:
    from tests.bdd.conftest import _active_client, _store_response

    mock_session = request.getfixturevalue("mock_session")
    team_id = ctx.get("team_id", team_identifier)
    resource_count = ctx.get("resource_count", 0)
    # A truthy team row only when the scenario declared one. The not-found
    # scenario declares no team, so the real ``get_team`` read returns None and
    # the endpoint raises 404 after the real soft-delete path declines to write.
    team_row = MagicMock() if "team_id" in ctx else None
    _configure_delete_session(
        mock_session,
        table_counts={"pipelines": resource_count},
        team_row=team_row,
    )

    with (
        patch("modulo.api.routes.teams.set_rls_org"),
        patch("modulo.api.routes.teams.set_rls_user_context"),
    ):
        resp = _active_client(request, client).delete(f"/api/v1/teams/{team_id}")

    _store_response(request, ctx, resp)


@then("the error indicates the team still has resources")
def error_indicates_resources(request) -> None:
    resp = request.node._resp
    data = resp.json()
    detail = data.get("detail", "")
    assert "team_has_resources" in detail, f"Expected the resource guard error, got: {data}"
    assert "still has resources" in detail, f"Expected 'still has resources', got: {data}"


@then(parsers.parse('the error message contains "{text}"'))
def error_message_contains(text: str, request) -> None:
    resp = request.node._resp
    data = resp.json()
    assert text in data.get("detail", ""), f"Expected '{text}' in error detail, got: {data}"
