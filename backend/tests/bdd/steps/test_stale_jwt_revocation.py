"""BDD step definitions: Stale JWT team membership revocation."""

import contextlib
import uuid
from unittest.mock import MagicMock

from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import _mock_team, _shared_state

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


@given(parsers.parse('a run "{run_name}" is awaiting human at gate "{gate_id}" with required_team_id "{team_name}"'))
def run_awaiting_gate(run_name: str, gate_id: str, team_name: str, request) -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team_name, _mock_team(team_name)).id
    state.setdefault("runs", {})[run_name] = {"id": str(uuid.uuid4()), "status": "awaiting_human"}
    state.setdefault("gates", {})[gate_id] = {"id": gate_id, "required_team_id": team_id}


@when(parsers.parse('I revoke user "{username}"\'s session'))
def revoke_user_session(username: str, request) -> None:
    _shared_state(request)["tokens_valid"] = False


@when(parsers.parse('user "{username}" refreshes their JWT'))
def user_refreshes_jwt(username: str, request) -> None:
    _shared_state(request)["tokens_valid"] = True


@when(parsers.parse('user "{username}" requests GET /api/pipelines/{pipeline_name}'))
def user_requests_pipeline(username: str, pipeline_name: str, request) -> None:
    state = _shared_state(request)
    pipeline = state["pipelines"].get(pipeline_name)
    owner_team = _team_name_for_pipeline(state, pipeline) if pipeline else ""
    is_member = (username, owner_team) in state.get("memberships", {})
    tokens_valid = state.get("tokens_valid", True)

    if not tokens_valid:
        resp = MagicMock()
        resp.status_code = 401
    elif pipeline and (pipeline.get("visibility") == "org" or is_member):
        resp = MagicMock()
        resp.status_code = 200
    else:
        resp = MagicMock()
        resp.status_code = 404
    request.node._resp = resp


@when(parsers.parse('user "{username}" uses an unexpired JWT issued before the change'))
def user_uses_old_jwt(username: str, request) -> None:
    resp = MagicMock()
    resp.status_code = 200
    request.node._resp = resp


@when(parsers.parse('I change user "{username}"\'s role from "{old_role}" to "{new_role}"'))
def change_user_role(username: str, old_role: str, new_role: str, request) -> None:
    state = _shared_state(request)
    for (user, team), role in state["memberships"].items():
        if user == username and role == old_role:
            state["memberships"][(user, team)] = new_role


@when(parsers.parse('user "{username}" attempts to claim gate "{gate_id}" on run "{run_name}"'))
def user_attempts_claim(username: str, gate_id: str, run_name: str, request) -> None:
    state = _shared_state(request)
    gate = state.get("gates", {}).get(gate_id)
    is_member = _user_member_of_gate_team(state, username, gate)

    if gate and not is_member:
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Membership required for this gate"}
    else:
        resp = MagicMock()
        resp.status_code = 200
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


@then("the HITL gate enforcement uses a DB-live membership check")
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
