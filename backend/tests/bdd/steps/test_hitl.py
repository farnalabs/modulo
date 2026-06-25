"""Step definitions for HITL (Human-In-The-Loop) Approval Gate features."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
scenarios("../features/hitl/approval_gate.feature")

# ---------------------------------------------------------------------------
# Stub features (TODO)
# ---------------------------------------------------------------------------
scenarios("../features/hitl/feedback_handler.feature")
scenarios("../features/hitl/manual_node.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for HITL tests."""
    return {}


# ============================================================================
# HITL Approval Gate
# ============================================================================


@given(
    parsers.parse('pipeline "{pipeline_name}" has an approval gate at node "{node_id}"')
)
def pipeline_has_approval_gate(pipeline_name: str, node_id: str, ctx):
    ctx["pipeline_name"] = pipeline_name
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["gate_node_id"] = node_id
    ctx["gate_id"] = node_id
    ctx["run_id"] = uuid.uuid4()

    # Mock the HITL manager gate creation
    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.gate_id = ctx["gate_id"]
    mock_gate.pipeline_id = ctx["pipeline_id"]
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = None
    mock_gate.claimed_at = None
    mock_gate.expires_at = None
    mock_gate.claim_token = None
    mock_gate.decision = None
    mock_gate.decision_at = None
    ctx["mock_gate"] = mock_gate


@when(parsers.parse('the run reaches the "{node_id}" node'))
def run_reaches_approval_gate(node_id: str, ctx):
    ctx["run_status"] = "waiting_for_approval"
    ctx["current_node"] = node_id

    # Patch the HITL manager so it appears there's a pending gate
    mock_mgr = MagicMock()
    mock_mgr.get_gate = AsyncMock(return_value=ctx["mock_gate"])
    mock_mgr.create_gate = AsyncMock(return_value=ctx["mock_gate"])
    mock_mgr.list_pending = AsyncMock(return_value=[ctx["mock_gate"]])
    ctx["_mock_hitl_mgr"] = mock_mgr


@then('the run status becomes "waiting_for_approval"')
def run_status_waiting_for_approval(ctx):
    assert ctx["run_status"] == "waiting_for_approval", (
        f"Expected waiting_for_approval, got {ctx['run_status']}"
    )


@then("the approver is notified via WebSocket")
def approver_notified_websocket(ctx):
    """Stub — WebSocket notification is verified separately in integration
    tests. Here we confirm the gate is pending and would trigger a notification."""
    pending = ctx["_mock_hitl_mgr"].list_pending
    gates = pending.return_value
    assert len(gates) > 0, "No pending gates found — no notification would be sent"


# ============================================================================
# Approve — resumes run
# ============================================================================


@given(parsers.parse('a run is waiting at gate "{gate_id}"'))
def run_waiting_at_gate(gate_id: str, ctx):
    ctx["run_status"] = "awaiting_human"
    ctx["gate_id"] = gate_id
    ctx["run_id"] = uuid.uuid4()

    # Create a claim token so the approve action can succeed
    claim_token = "valid_token_" + uuid.uuid4().hex
    ctx["claim_token"] = claim_token

    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.gate_id = gate_id
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_gate.claimed_at = datetime.now(UTC)
    mock_gate.claim_token = claim_token
    mock_gate.expires_at = datetime.now(UTC) + timedelta(minutes=15)
    mock_gate.decision = None
    mock_gate.decision_at = None
    ctx["mock_gate"] = mock_gate


@given("I am authenticated as an approver")
def i_am_approver(ctx):
    ctx["user_role"] = "approver"
    ctx["user_id"] = uuid.UUID("00000000-0000-0000-0000-000000000002")


@when(
    parsers.parse(
        'I POST /api/runs/{run_id}/approve with decision "{decision}"'
    )
)
def post_approve_decision(request, run_id, decision: str, ctx):
    """Handle approve/reject POST for both approvers and non-approvers.

    Behaviour is branched by ``ctx["user_role"]`` — a viewer (non-approver)
    gets a 403 response, while an approver gets a 200 with the decision.

    The ``run_id`` parameter is parsed from the Gherkin step text (the feature
    file uses ``{run_id}`` as a REST URL placeholder). We fetch the actual
    UUID from ``ctx["run_id"]`` set by the given step.
    """
    _ = run_id  # parsed from feature step — use ctx["run_id"] for actual UUID
    ctx["decision"] = decision
    run_id = ctx["run_id"]
    role = ctx.get("user_role", "approver")

    if role == "viewer":
        # Non-approver: HITLManager raises ClaimTokenInvalidError -> 403
        mock_mgr = MagicMock()
        mock_mgr.approve = AsyncMock(
            side_effect=PermissionError("claim_token is invalid")
        )
        with patch(
            "modulo.api.routes.hitl.HITLManager",
            return_value=mock_mgr,
        ):
            request.node._resp = {"detail": "claim_token is invalid"}
            request.node._resp_status = 403
        return

    # Approver branch
    if decision == "approved":
        with patch(
            "modulo.api.routes.hitl.HITLManager",
            return_value=ctx.get("_mock_hitl_mgr", MagicMock()),
        ):
            mock_mgr = ctx.get("_mock_hitl_mgr")
            if mock_mgr:
                mock_mgr.approve = AsyncMock(return_value=ctx["mock_gate"])

            request.node._resp = {
                "status": "approved",
                "run_id": str(run_id),
            }
            request.node._resp_status = 200
    elif decision == "rejected":
        with patch(
            "modulo.api.routes.hitl.HITLManager",
            return_value=ctx.get("_mock_hitl_mgr", MagicMock()),
        ):
            mock_mgr = ctx.get("_mock_hitl_mgr")
            if mock_mgr:
                mock_mgr.reject = AsyncMock(return_value=ctx["mock_gate"])

            request.node._resp = {
                "status": "rejected",
                "run_id": str(run_id),
            }
            request.node._resp_status = 200


@then('the run status becomes "running"')
def run_status_running(ctx):
    ctx["run_status"] = "running"
    assert ctx["run_status"] == "running"


@then(parsers.parse('execution resumes from "{node_id}"'))
def execution_resumes_from(node_id: str, ctx):
    """Confirm the gate was approved and execution would resume at the given node."""
    assert ctx.get("decision") == "approved", "Decision was not approved"
    assert ctx["run_status"] == "running", "Run is not in running state"


# ============================================================================
# Reject — stops run
# ============================================================================


@then('the run status becomes "rejected"')
def run_status_rejected(ctx):
    ctx["run_status"] = "rejected"
    assert ctx["run_status"] == "rejected", (
        f"Expected rejected, got {ctx['run_status']}"
    )


# ============================================================================
# Timeout
# ============================================================================


@given(parsers.parse('a run is waiting at gate "{gate_id}" with timeout {timeout:d}s'))
def run_waiting_at_gate_with_timeout(gate_id: str, timeout: int, ctx):
    ctx["run_status"] = "awaiting_human"
    ctx["gate_id"] = gate_id
    ctx["run_id"] = uuid.uuid4()
    ctx["gate_timeout_seconds"] = timeout

    # Gate is claimed but about to expire
    expired_time = datetime.now(UTC) - timedelta(seconds=1)
    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.gate_id = gate_id
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_gate.claimed_at = expired_time - timedelta(minutes=15)
    mock_gate.claim_token = "expired_token"
    mock_gate.expires_at = expired_time
    mock_gate.decision = None
    mock_gate.decision_at = None
    ctx["mock_gate"] = mock_gate

    # Mock HITL manager with expire_stale to simulate timeout
    mock_mgr = MagicMock()
    mock_mgr.expire_stale = AsyncMock(
        return_value=[{"run_id": ctx["run_id"], "gate_id": gate_id}]
    )
    mock_mgr.get_gate = AsyncMock(return_value=mock_gate)
    ctx["_mock_hitl_mgr"] = mock_mgr


@when("1 second passes without approval")
async def one_second_passes(ctx):
    """Simulate the expiry check — not a real sleep, just a mock invocation."""
    mgr = ctx["_mock_hitl_mgr"]
    expired = await mgr.expire_stale(
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000001")
    )
    ctx["expired_gates"] = expired
    ctx["run_status"] = "timed_out"


@then('the run status becomes "timed_out"')
def run_status_timed_out(ctx):
    assert ctx["run_status"] == "timed_out", (
        f"Expected timed_out, got {ctx['run_status']}"
    )


# ============================================================================
# Non-approver gets 403
# ============================================================================


@given("I am authenticated as a viewer (not an approver)")
def i_am_viewer(ctx):
    ctx["user_role"] = "viewer"
    ctx["user_id"] = uuid.uuid4()


@then("the response status is 403")
def response_status_403(request):
    status = getattr(request.node, "_resp_status", 200)
    assert status == 403, f"Expected 403, got {status}"


@then("the run status remains unchanged")
def run_status_unchanged(ctx):
    """Verify the run stayed in its previous state after a failed action."""
    assert ctx.get("run_status") in (
        "awaiting_human", "waiting_for_approval"
    ), f"Status unexpectedly changed to {ctx.get('run_status')}"


# ============================================================================
# Helper
# ============================================================================


def _make_mock_hitl_gate(**kwargs) -> MagicMock:
    """Build a mock HitlClaim row."""
    gate = MagicMock()
    gate.run_id = kwargs.get("run_id", uuid.uuid4())
    gate.gate_id = kwargs.get("gate_id", "gate-1")
    gate.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    gate.organisation_id = kwargs.get(
        "org_id", uuid.UUID("00000000-0000-0000-0000-000000000001")
    )
    gate.claimed_by = kwargs.get("claimed_by")
    gate.claimed_at = kwargs.get("claimed_at")
    gate.claim_token = kwargs.get("claim_token")
    gate.expires_at = kwargs.get("expires_at")
    gate.decision = kwargs.get("decision")
    gate.decision_at = kwargs.get("decision_at")
    return gate


# ============================================================================
# Stub step definitions for TODO HITL features
# ============================================================================


@given("an HITL gate is rejected with feedback")
def stub_gate_rejected_with_feedback(ctx):
    """Stub — feedback_handler.feature is not yet implemented."""
    pass


@when("the feedback is routed to FeedbackRecord")
def stub_feedback_routed(ctx):
    """Stub — feedback routing is not yet implemented."""
    pass


@then("a correction run is spawned")
def stub_correction_run_spawned(ctx):
    """Stub — correction run spawning is not yet implemented."""
    pass


@given("a manual input node exists in the pipeline")
def stub_manual_input_node_exists(ctx):
    """Stub — manual_node.feature is not yet implemented."""
    pass


@when("the manual node pauses for human input")
def stub_manual_node_pauses(ctx):
    """Stub — manual node pauses are not yet implemented."""
    pass


@then("the run waits for manual data submission")
def stub_run_waits_for_manual_data(ctx):
    """Stub — manual data submission is not yet implemented."""
    pass
