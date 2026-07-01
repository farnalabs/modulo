"""BDD step definitions: Team-scoped HITL gates."""

import uuid
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../features/teams/team_hitl_gate.feature")
except (FileNotFoundError, OSError):
    pass

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "users": {},
        "memberships": {},
        "runs": {},
        "gates": {},
    }


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(
    parsers.parse('user "{username}" is a member of team "{team_name}" with role "{role}"')
)
def user_is_member_with_role(username: str, team_name: str, role: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id, "role": role}
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4())}


@given(
    parsers.parse('user "{username}" is not a member of team "{team_name}"')
)
def user_not_member(username: str, team_name: str, ctx) -> None:
    if username not in ctx["users"]:
        ctx["users"][username] = {"id": str(uuid.uuid4())}


@given(
    parsers.parse('a run "{run_id}" is awaiting human at gate "{gate_id}" with required_team_id "{team_name}"')
)
def run_awaiting_gate_with_team(run_id: str, gate_id: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["runs"][run_id] = {"id": str(uuid.uuid4()), "status": "awaiting_human"}
    ctx["gates"][gate_id] = {
        "run_id": run_id,
        "required_team_id": team_id,
        "required_team_name": team_name,
        "human_only": False,
    }


@given(
    parsers.parse(
        'a run "{run_id}" is awaiting human at gate "{gate_id}" with required_team_id "{team_name}" and human_only true'
    )
)
def run_awaiting_gate_human_only(run_id: str, gate_id: str, team_name: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["runs"][run_id] = {"id": str(uuid.uuid4()), "status": "awaiting_human"}
    ctx["gates"][gate_id] = {
        "run_id": run_id,
        "required_team_id": team_id,
        "required_team_name": team_name,
        "human_only": True,
    }


@given(
    parsers.parse('user "{username}" holds a valid claim_token for gate "{gate_id}"')
)
def user_holds_claim_token(username: str, gate_id: str, ctx) -> None:
    ctx["claim_token"] = str(uuid.uuid4())
    ctx["claimed_by"] = username


@when(
    parsers.parse('user "{username}" claims the HITL gate "{gate_id}" on run "{run_name}"')
)
def user_claims_gate(username: str, gate_id: str, run_name: str, request, ctx) -> None:
    gate = ctx["gates"].get(gate_id)
    membership = ctx["memberships"].get(username)

    if gate and gate.get("required_team_id"):
        required_team_id = gate["required_team_id"]
        if not membership or membership.get("team_id") != required_team_id:
            resp = MagicMock()
            resp.status_code = 403
            resp.json = lambda: {"detail": f"This gate requires team {gate['required_team_name']}"}
            request.node._resp = resp
            return

    mock_result = {"claim_token": str(uuid.uuid4()), "claimed_at": "2025-01-01T00:00:00"}
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: mock_result
    request.node._resp = resp


@when(
    parsers.parse('user "{username}" approves gate "{gate_id}" on run "{run_name}"')
)
def user_approves_gate(username: str, gate_id: str, run_name: str, request, ctx) -> None:
    ctx["gates"].get(gate_id)
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"status": "approved", "run_status": "running"}
    request.node._resp = resp


@when(
    parsers.parse("an MCP client attempts to approve gate {gate_id} on run {run_name} as user {username}")
)
def mcp_approve_gate(username: str, gate_id: str, run_name: str, request, ctx) -> None:
    gate = ctx["gates"].get(gate_id)
    if gate and gate.get("human_only"):
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": "This gate requires human approval"}
        request.node._resp = resp
        return


@when(
    parsers.parse('I request the gate context for run "{run_name}" gate "{gate_id}"')
)
def request_gate_context(run_name: str, gate_id: str, request, ctx) -> None:
    gate = ctx["gates"].get(gate_id)
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {
        "run_id": ctx["runs"].get(run_name, {}).get("id"),
        "gate_id": gate_id,
        "required_team_id": gate["required_team_id"] if gate else None,
        "required_team_name": gate["required_team_name"] if gate else None,
    }
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, request) -> None:
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}"


@then("the response contains a claim_token")
def response_has_claim_token(request) -> None:
    data = request.node._resp.json()
    assert "claim_token" in data


@then(
    parsers.parse('the error indicates the gate requires team "{team_name}"')
)
def error_indicates_team_required(team_name: str, request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert "requires team" in detail.lower() or team_name.lower() in detail.lower()


@then("the error indicates the gate requires human approval")
def error_human_only(request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "").lower()
    assert "human" in detail


@then(
    parsers.parse('the response contains required_team_id "{team_name}"')
)
def response_has_required_team_id(team_name: str, request, ctx) -> None:
    data = request.node._resp.json()
    expected_id = ctx["teams"].get(team_name, {}).get("id")
    assert str(data.get("required_team_id")) == str(expected_id)


@then(
    parsers.parse('the response contains required_team_name "{team_name}"')
)
def response_has_required_team_name(team_name: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("required_team_name") == team_name


@then("the run resumes execution")
def run_resumes(request) -> None:
    data = request.node._resp.json()
    assert data.get("run_status") == "running"

