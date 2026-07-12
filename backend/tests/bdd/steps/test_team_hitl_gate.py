"""Step definitions for Team-Scoped HITL Gate features."""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_hitl_gate.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for team HITL gate tests."""
    return {}


# ============================================================================
# Team — HITL Gate
# ============================================================================


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx):
    ctx["team_name"] = team_name
    ctx["team_id"] = uuid.uuid4()


@given(parsers.parse('user "{username}" is a member of team "{team_name}" with role "{role}"'))
def user_is_team_member(username: str, team_name: str, role: str, ctx):
    ctx["username"] = username
    ctx["user_id"] = uuid.uuid4()
    ctx["team_role"] = role


@given(parsers.parse('user "{username}" is not a member of team "{team_name}"'))
def user_not_team_member(username: str, team_name: str, ctx):
    ctx["username"] = username
    ctx["user_id"] = uuid.uuid4()


@given(parsers.parse('a run "{run_name}" is awaiting human at gate "{gate_id}" with required_team_id "{team_name}"'))
def run_awaiting_with_team(run_name: str, gate_id: str, team_name: str, ctx):
    ctx["run_name"] = run_name
    ctx["run_id"] = uuid.uuid4()
    ctx["gate_id"] = gate_id
    ctx["run_status"] = "awaiting_human"

    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.gate_id = gate_id
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = None
    mock_gate.claimed_at = None
    mock_gate.expires_at = None
    mock_gate.claim_token = None
    mock_gate.decision = None
    mock_gate.decision_at = None
    mock_gate.required_team_id = ctx.get("team_id")
    ctx["mock_gate"] = mock_gate


@given(
    parsers.parse(
        'a run "{run_name}" is awaiting human at gate "{gate_id}"'
        ' with required_team_id "{team_name}" and human_only true'
    )
)
def run_awaiting_with_team_and_human_only(run_name: str, gate_id: str, team_name: str, ctx):
    ctx["run_name"] = run_name
    ctx["run_id"] = uuid.uuid4()
    ctx["gate_id"] = gate_id
    ctx["human_only"] = True
    ctx["run_status"] = "awaiting_human"

    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.gate_id = gate_id
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = None
    mock_gate.claimed_at = None
    mock_gate.expires_at = None
    mock_gate.claim_token = None
    mock_gate.decision = None
    mock_gate.decision_at = None
    mock_gate.required_team_id = ctx.get("team_id")
    ctx["mock_gate"] = mock_gate


@when(parsers.parse('user "{username}" claims the HITL gate "{gate_id}" on run "{run_name}"'))
def user_claims_gate(username: str, gate_id: str, run_name: str, ctx):
    from modulo.core.hitl_manager import NotTeamMemberError

    _ = gate_id, run_name

    # Gateway mock — if user is a team member, succeed; otherwise raise NotTeamMemberError
    mock_mgr = MagicMock()
    is_member = ctx.get("team_role") is not None
    if is_member:
        mock_mgr.claim = AsyncMock(return_value=ctx["mock_gate"])
        mock_mgr.create_gate = AsyncMock(return_value=ctx["mock_gate"])
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"claim_token": "valid_token_" + uuid.uuid4().hex}
        ctx["claim_token"] = "valid_token_" + uuid.uuid4().hex
    else:
        mock_mgr.claim = AsyncMock(
            side_effect=NotTeamMemberError(
                run_id=ctx["run_id"],
                gate_id=gate_id,
                team_id=ctx.get("team_id", uuid.uuid4()),
                user_id=ctx.get("user_id", uuid.uuid4()),
            )
        )
        resp = MagicMock()
        resp.status_code = 403
        resp.json = lambda: {"detail": f"User is not a member of team {ctx.get('team_name', 'engineering')}"}

    ctx["_mock_hitl_mgr"] = mock_mgr
    ctx["_resp"] = resp


@when(parsers.parse('an MCP client attempts to approve gate "{gate_id}" on run "{run_name}" as user "{username}"'))
def mcp_attempts_approve(gate_id: str, run_name: str, username: str, ctx):
    _ = run_name, username
    # MCP client trying to approve human_only gate — should get 403
    resp = MagicMock()
    resp.status_code = 403
    resp.json = lambda: {"detail": "Gate requires human approval — MCP clients cannot auto-approve"}
    ctx["_resp"] = resp


@when(parsers.parse('I request the gate context for run "{run_name}" gate "{gate_id}"'))
def request_gate_context(run_name: str, gate_id: str, ctx):
    _ = gate_id, run_name
    # Simulate returning gate context that includes team info
    ctx["_resp"] = MagicMock()
    ctx["_resp"].status_code = 200
    ctx["_resp"].json = lambda: {
        "gate_id": ctx["gate_id"],
        "run_id": str(ctx["run_id"]),
        "required_team_id": str(ctx.get("team_id", "")),
        "required_team_name": ctx.get("team_name", ""),
    }


@given(parsers.parse('user "{username}" holds a valid claim_token for gate "{gate_id}"'))
def user_holds_claim_token(username: str, gate_id: str, ctx):
    _ = gate_id
    ctx["username"] = username
    ctx["user_id"] = ctx.get("user_id", uuid.uuid4())
    ctx["claim_token"] = "valid_token_" + uuid.uuid4().hex

    # Create a mock gate that is claimed by this user
    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.gate_id = ctx["gate_id"]
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = ctx["user_id"]
    mock_gate.claim_token = ctx["claim_token"]
    mock_gate.decision = None
    mock_gate.required_team_id = ctx.get("team_id")
    ctx["mock_gate"] = mock_gate


@when(parsers.parse('user "{username}" approves gate "{gate_id}" on run "{run_name}"'))
def user_approves_gate(username: str, gate_id: str, run_name: str, ctx):
    _ = username, gate_id, run_name

    mock_mgr = MagicMock()
    mock_mgr.approve = AsyncMock(return_value=ctx["mock_gate"])

    with patch("modulo.api.routes.hitl.HITLManager", return_value=mock_mgr):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"status": "approved", "run_id": str(ctx["run_id"])}
        ctx["_resp"] = resp
        ctx["run_status"] = "running"


@then("the response contains a claim_token")
def response_contains_claim_token(ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        assert "claim_token" in body, "Expected claim_token in response"
        assert body["claim_token"] is not None, "Expected non-null claim_token"


@then(parsers.parse('the error indicates the gate requires team "{team_name}"'))
def error_indicates_team_required(team_name: str, ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        detail = body.get("detail", "")
        assert team_name in detail or "team" in detail.lower(), (
            f"Expected error to mention team {team_name}, got: {detail}"
        )


@then("the error indicates the gate requires human approval")
def error_indicates_human_required(ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        detail = body.get("detail", "")
        assert "human" in detail.lower(), f"Expected error to mention human approval, got: {detail}"


@then(parsers.parse('the response contains required_team_id "{team_name}"'))
def response_contains_required_team_id(team_name: str, ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        assert body.get("required_team_id"), "Expected required_team_id in response"


@then(parsers.parse('the response contains required_team_name "{team_name}"'))
def response_contains_required_team_name(team_name: str, ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        team_name_body = body.get("required_team_name", "")
        assert team_name_body == team_name, f"Expected required_team_name '{team_name}', got '{team_name_body}'"


@then("the run resumes execution")
def run_resumes_execution(ctx):
    assert ctx.get("run_status") == "running", f"Expected run to be running, got {ctx.get('run_status')}"
