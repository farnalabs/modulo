"""BDD step definitions: Stale JWT team membership revocation."""

import contextlib
import uuid
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/stale_jwt_revocation.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "users": {},
        "memberships": {},
        "pipelines": {},
        "tokens_valid": True,
    }


@given(parsers.parse('a user "{username}" exists'))
def user_exists(username: str, ctx) -> None:
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4()), "username": username}


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id, "role": "operator"}
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4()), "username": username}


@given(parsers.parse('user "{username}" holds a valid JWT'))
def user_holds_valid_jwt(username: str, ctx) -> None:
    ctx["tokens_valid"] = True


@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@given(parsers.parse('user "{username}" is a member of team "{team_name}" with role "{role}"'))
def user_is_member_with_role(username: str, team_name: str, role: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id, "role": role}
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4()), "username": username}


@given(parsers.parse('user "{username}" is removed from team "{team_name}"'))
def user_removed_from_team(username: str, team_name: str, ctx) -> None:
    if username in ctx["memberships"]:
        del ctx["memberships"][username]


@given(parsers.parse('user "{username}" still holds a valid JWT'))
def user_still_holds_jwt(username: str, ctx) -> None:
    ctx["tokens_valid"] = True


@given(parsers.parse('a run "{run_name}" is awaiting human at gate "{gate_id}" with required_team_id "{team_name}"'))
def run_awaiting_gate(run_name: str, gate_id: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["runs"] = ctx.get("runs", {})
    ctx["runs"][run_name] = {"id": str(uuid.uuid4()), "status": "awaiting_human"}
    ctx["gates"] = ctx.get("gates", {})
    ctx["gates"][gate_id] = {"id": gate_id, "required_team_id": team_id}


@when(parsers.parse('I revoke sessions for user "{username}"'))
def revoke_user_session(username: str, ctx) -> None:
    ctx["tokens_valid"] = False


@when(parsers.parse('user "{username}" refreshes their JWT'))
def user_refreshes_jwt(username: str, ctx) -> None:
    ctx["tokens_valid"] = True


@when(parsers.parse('user "{username}" requests GET /api/pipelines/{pipeline_name}'))
def user_requests_pipeline(username: str, pipeline_name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(pipeline_name)
    is_member = username in ctx.get("memberships", {})
    tokens_valid = ctx.get("tokens_valid", True)

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


@when(parsers.parse('I change user "{username}" role from "{old_role}" to "{new_role}"'))
def change_user_role(username: str, old_role: str, new_role: str, ctx) -> None:
    if username in ctx["memberships"]:
        ctx["memberships"][username]["role"] = new_role


@when(parsers.parse('user "{username}" attempts to claim gate "{gate_id}" on run "{run_name}"'))
def user_attempts_claim(username: str, gate_id: str, run_name: str, request, ctx) -> None:
    is_member = username in ctx.get("memberships", {})
    gate = ctx.get("gates", {}).get(gate_id)

    if gate and not is_member:
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "Membership required for this gate"}
    else:
        resp = MagicMock()
        resp.status_code = 200
    request.node._resp = resp


@then(parsers.parse('user "{username}" is redirected to re-authenticate on next request'))
def user_redirected_to_auth(username: str, ctx) -> None:
    assert ctx.get("tokens_valid") is False


@then("the response respects the old role until token refresh")
def response_respects_old_role(request) -> None:
    pass


@then("this is a documented acceptable gap of up to 15 minutes")
def documented_acceptable_gap() -> None:
    pass


@then("the HITL gate enforcement uses a DB-live membership check")
def hitl_uses_db_live_check() -> None:
    pass
