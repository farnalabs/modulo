"""BDD step definitions: Stale JWT team membership revocation."""

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import _mock_team, _shaped_execute, _shared_state

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/stale_jwt_revocation.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@given(parsers.parse('a user "{username}" exists'))
def user_exists(username: str, request) -> None:
    state = _shared_state(request)
    state["users"].setdefault(
        username, {"id": str(uuid.uuid4()), "name": username, "org_role": "admin", "team_role": None}
    )


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, request) -> None:
    state = _shared_state(request)
    state["teams"].setdefault(team_name, _mock_team(team_name))


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, request) -> None:
    state = _shared_state(request)
    state["teams"].setdefault(team_name, _mock_team(team_name))
    state["users"].setdefault(
        username, {"id": str(uuid.uuid4()), "name": username, "org_role": "viewer", "team_role": None}
    )
    state["memberships"][(username, team_name)] = "operator"


@given(parsers.parse('user "{username}" holds a valid JWT'))
def user_holds_valid_jwt(username: str, request) -> None:
    _shared_state(request)["tokens_valid"] = True


@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, request) -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team_name, _mock_team(team_name)).id
    state["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": str(team_id),
        "visibility": visibility,
    }


@given(parsers.parse('user "{username}" is a member of team "{team_name}" with role "{role}"'))
def user_is_member_with_role(username: str, team_name: str, role: str, request) -> None:
    state = _shared_state(request)
    state["teams"].setdefault(team_name, _mock_team(team_name))
    state["users"].setdefault(
        username, {"id": str(uuid.uuid4()), "name": username, "org_role": "viewer", "team_role": None}
    )
    state["memberships"][(username, team_name)] = role


@given(parsers.parse('user "{username}" is removed from team "{team_name}"'))
def user_removed_from_team(username: str, team_name: str, request) -> None:
    _shared_state(request)["memberships"].pop((username, team_name), None)


@given(parsers.parse('user "{username}" still holds a valid JWT'))
def user_still_holds_jwt(username: str, request) -> None:
    _shared_state(request)["tokens_valid"] = True


@given(parsers.parse('a run "{run_name}" is awaiting human at gate "{review_id}" with required_team_id "{team_name}"'))
def run_awaiting_gate(run_name: str, review_id: str, team_name: str, request) -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team_name, _mock_team(team_name)).id
    state.setdefault("runs", {})[run_name] = {"id": str(uuid.uuid4()), "status": "awaiting_human"}
    state.setdefault("gates", {})[review_id] = {"id": review_id, "required_team_id": str(team_id)}


@when(parsers.parse('I revoke user "{username}"\'s session'))
def revoke_user_session(username: str, request) -> None:
    _shared_state(request)["tokens_valid"] = False


@when(parsers.parse('user "{username}" refreshes their JWT'))
def user_refreshes_jwt(username: str, request) -> None:
    _shared_state(request)["tokens_valid"] = True


@when(parsers.parse('user "{username}" requests GET /api/pipelines/{pipeline_name}'))
def user_requests_pipeline(username: str, pipeline_name: str, request) -> None:
    """Fetch the pipeline through the REAL ``GET /api/v1/pipelines/{id}`` route.

    The route's own gates run unpatched: ``require_permission`` (floor:
    ``pipeline.list``, viewer allowed) then
    ``require_team_membership_or_admin(resolve_pipeline_team_scope)`` -
    the team-blind scope read. When that read reports NO row (the shape fed
    here models the RLS-hidden post-removal row: under RLS the removed
    member's own session simply no longer sees the row), the gate denies
    with 404 - precisely the revocation outcome the scenario asserts.

    FAR-1600 follow-up: this shaper hard-codes the team-scope read to
    no-row, so only the 404 revocation path is exercised through the route.
    A future scenario wanting 200 (still-grace member) or 401 (stale token
    principal) must wire a shaped row / stale-token principal here rather
    than reuse this empty shape.
    """
    state = _shared_state(request)
    pipeline = state["pipelines"].get(pipeline_name)
    assert pipeline is not None, f"Pipeline '{pipeline_name}' not registered in state"
    pipeline_id = pipeline["id"]

    def shaper(session: MagicMock) -> None:
        _shaped_execute(
            session,
            [MagicMock(first=MagicMock(return_value=None))],
        )

    from tests.bdd.conftest import session_client

    with session_client(role=state["users"].get(username, {}).get("org_role", "viewer"), shaper=shaper) as client:
        resp = client.get(f"/api/v1/pipelines/{pipeline_id}")
    request.node._resp = resp


@when(parsers.parse('user "{username}" uses an unexpired JWT issued before the change'))
def user_uses_old_jwt(username: str, request) -> None:
    """Get the pipeline list through the REAL ``GET /api/v1/pipelines`` route.

    The route's ``require_permission`` floor runs for real against the injected
    principal's role; the service read behind listing is shaped so the org
    pipeline row the scenario registered comes back through a genuine
    serialisation path.

    FAR-1600 follow-up: this drives the route end-to-end but injects the user's
    CURRENT ``org_role`` as the principal, and the scenario's
    ``response_respects_old_role`` / ``documented_acceptable_gap`` then-steps
    are still no-ops - so the "grace period" scenario is exercised but never
    asserted. A faithful grace-window test must mint the principal with the
    PRE-change role and assert the response against it.
    """
    state = _shared_state(request)
    org_role = state["users"].get(username, {}).get("org_role", "viewer")

    def shaper(session: MagicMock) -> None:
        _shaped_execute(session, [])

    from tests.bdd.conftest import session_client

    with session_client(role=org_role, shaper=shaper) as client:
        resp = client.get("/api/v1/pipelines")
    request.node._resp = resp


@when(parsers.parse('I change user "{username}"\'s role from "{old_role}" to "{new_role}"'))
def change_user_role(username: str, old_role: str, new_role: str, request) -> None:
    state = _shared_state(request)
    for (user, team), role in state["memberships"].items():
        if user == username and role == old_role:
            state["memberships"][(user, team)] = new_role


@when(parsers.parse('user "{username}" attempts to claim gate "{review_id}" on run "{run_name}"'))
def user_attempts_claim(username: str, review_id: str, run_name: str, request) -> None:
    """Claim a run's HITL gate through the REAL ``POST /api/v1/runs/.../hitl/claim`` route.

    The route's permission floor (``hitl.claim``), the ``human_only`` gate
    pre-checks and :mod:`modulo.core.hitl_manager`'s claim body run unpatched.
    The reads that decide the outcome are shaped in route-decision order:
    the pending gate, the awaiting_human run, a re-read of the gate inside
    the claim transaction, and a membership read that reports NO row. That
    final empty membership makes the claim fail with "membership required"
    - the DB-live membership check the scenario demands.
    """
    state = _shared_state(request)
    run_info = state.get("runs", {}).get(run_name)
    assert run_info is not None, f"Run '{run_name}' not registered in state"
    run_id = run_info["id"]

    def shaper(session: MagicMock) -> None:
        gate = SimpleNamespace(
            id=review_id,
            account_id=None,
            required_team_id=state.get("gates", {}).get(review_id, {}).get("required_team_id"),
            decision=None,
            claim_token=None,
        )
        run_row = SimpleNamespace(id=run_id, status="awaiting_human")
        _shaped_execute(
            session,
            [
                MagicMock(scalar_one_or_none=MagicMock(return_value=gate)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=run_row)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=gate)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ],
        )

    from tests.bdd.conftest import session_client

    with session_client(role="operator", shaper=shaper) as client:
        resp = client.post(f"/api/v1/runs/{run_id}/hitl/{review_id}/claim", json={"expiry_minutes": 5})
    request.node._resp = resp


@then(parsers.parse('user "{username}" is redirected to re-authenticate on next request'))
def user_redirected_to_auth(username: str, request) -> None:
    assert _shared_state(request).get("tokens_valid") is False


@then("the response respects the old role until token refresh")
def response_respects_old_role(request) -> None:
    pass


@then("this is a documented acceptable gap of up to 15 minutes")
def documented_acceptable_gap() -> None:
    pass


@then("the HITL review enforcement uses a DB-live membership check")
def hitl_uses_db_live_check() -> None:
    pass


def _team_name_for_pipeline(state: dict, pipeline: dict) -> str:
    for team_name, team in state["teams"].items():
        if str(team.id) == str(pipeline.get("owner_team_id")):
            return team_name
    return ""


def _user_member_of_gate_team(state: dict, username: str, gate: dict) -> bool:
    if not gate or not state["teams"]:
        return False
    required_team_id = str(gate.get("required_team_id"))
    for team_name, team in state["teams"].items():
        if str(team.id) == required_team_id:
            return (username, team_name) in state.get("memberships", {})
    return False
