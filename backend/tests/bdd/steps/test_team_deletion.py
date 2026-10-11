"""BDD step definitions: Team deletion workflow.

Re-anchored to the REAL ``delete_team_endpoint``: only the DB/RLS seams
(``set_rls_org`` / ``set_rls_user_context``) are patched, so the endpoint's own
resource-count guard and the real ``delete_team`` soft-delete path run end to
end. The shared ``mock_session`` is wired entity-aware (see
``team_deletion_support``) so each scenario blocks on its own resource type and
the 404 path is driven by a real ``get_team`` miss.
"""

import contextlib
import uuid
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.steps.team_deletion_support import _configure_delete_session

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_deletion.feature")


@pytest.fixture
def ctx():
    """Shared mutable context dict for team deletion tests."""
    return {}


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
    # the endpoint raises 404 with "Team not found" after the real soft-delete
    # path declines to write.
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


@then(parsers.parse('the team "{team_name}" is no longer retrievable'))
def deleted_team_not_retrievable(team_name: str, request, ctx, client=None) -> None:
    """A deleted team no longer answers ``GET /api/v1/teams/{id}``.

    ``delete_team`` is a SOFT delete (``team.deleted_at = now``) and the real
    ``get_team`` read filters ``Team.deleted_at.is_(None)`` — so after a 204
    the lookup runs the genuine ``get_team_endpoint`` against a session whose
    ``teams`` read encodes the post-delete state (no live row) and returns the
    endpoint's real 404 detail, ``Team not found``. NOTE: team memberships are
    NOT physically removed by the soft delete (the FK cascade is hard-delete
    only); this scenario asserts the lookup contract, not membership cleanup.
    """
    from tests.bdd.conftest import _active_client, _store_response

    mock_session = request.getfixturevalue("mock_session")
    team_id = ctx.get("team_id")
    assert team_id, f"No team was deleted for '{team_name}'"
    # The row is gone from get_team's perspective: deleted_at is now set.
    _configure_delete_session(mock_session, table_counts={}, team_row=None)

    with (
        patch("modulo.api.routes.teams.set_rls_org"),
        patch("modulo.api.routes.teams.set_rls_user_context"),
    ):
        resp = _active_client(request, client).get(f"/api/v1/teams/{team_id}")
    _store_response(request, ctx, resp)

    data = resp.json()
    assert resp.status_code == 404, f"Expected 404 after deletion, got {resp.status_code}: {data}"
    assert data.get("detail") == "Team not found", f"Expected 'Team not found' detail, got: {data}"


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
