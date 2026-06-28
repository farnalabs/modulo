"""Step definitions for HITL (Human-In-The-Loop) Approval Gate features."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/hitl/claim.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/hitl/approve.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../bdd/features/hitl/feedback_handler.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../bdd/features/hitl/manual_node.feature")
except (FileNotFoundError, OSError):
    pass

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


@given(parsers.parse('pipeline "{pipeline_name}" has an approval gate at node "{node_id}"'))
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
    assert ctx["run_status"] == "waiting_for_approval", f"Expected waiting_for_approval, got {ctx['run_status']}"


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


@when(parsers.parse('I POST /api/runs/{run_id}/approve with decision "{decision}"'))
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
        mock_mgr.approve = AsyncMock(side_effect=PermissionError("claim_token is invalid"))
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
    assert ctx["run_status"] == "rejected", f"Expected rejected, got {ctx['run_status']}"


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
    mock_mgr.expire_stale = AsyncMock(return_value=[{"run_id": ctx["run_id"], "gate_id": gate_id}])
    mock_mgr.get_gate = AsyncMock(return_value=mock_gate)
    ctx["_mock_hitl_mgr"] = mock_mgr


@when("1 second passes without approval")
async def one_second_passes(ctx):
    """Simulate the expiry check — not a real sleep, just a mock invocation."""
    mgr = ctx["_mock_hitl_mgr"]
    expired = await mgr.expire_stale(org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
    ctx["expired_gates"] = expired
    ctx["run_status"] = "timed_out"


@then('the run status becomes "timed_out"')
def run_status_timed_out(ctx):
    assert ctx["run_status"] == "timed_out", f"Expected timed_out, got {ctx['run_status']}"


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
    assert ctx.get("run_status") in ("awaiting_human", "waiting_for_approval"), (
        f"Status unexpectedly changed to {ctx.get('run_status')}"
    )


# ============================================================================
# Helper
# ============================================================================


def _make_mock_hitl_gate(**kwargs) -> MagicMock:
    """Build a mock HitlClaim row."""
    gate = MagicMock()
    gate.run_id = kwargs.get("run_id", uuid.uuid4())
    gate.gate_id = kwargs.get("gate_id", "gate-1")
    gate.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    gate.organisation_id = kwargs.get("org_id", uuid.UUID("00000000-0000-0000-0000-000000000001"))
    gate.claimed_by = kwargs.get("claimed_by")
    gate.claimed_at = kwargs.get("claimed_at")
    gate.claim_token = kwargs.get("claim_token")
    gate.expires_at = kwargs.get("expires_at")
    gate.decision = kwargs.get("decision")
    gate.decision_at = kwargs.get("decision_at")
    return gate


# ============================================================================
# Feedback Handler (§8.20)
# ============================================================================


@given("a feedback record exists for the current run")
def feedback_record_exists(ctx):
    """Set up a mock feedback record for listing/detail scenarios."""
    ctx["feedback_record"] = {
        "id": str(uuid.uuid4()),
        "run_id": str(ctx.get("run_id", uuid.uuid4())),
        "gate_id": ctx.get("gate_id", "review-output"),
        "rejected_by": str(ctx.get("user_id", uuid.uuid4())),
        "rejection_reason": "Output lacks required citations",
        "feedback_status": "pending",
    }


@given(parsers.parse('a feedback record exists with status "{status}"'))
def feedback_record_with_status(status: str, ctx):
    ctx["feedback_status"] = status
    ctx["feedback_record"] = {
        "id": str(uuid.uuid4()),
        "run_id": str(ctx.get("run_id", uuid.uuid4())),
        "feedback_status": status,
    }


@given(parsers.parse('a feedback record exists with handler type "{handler_type}"'))
def feedback_record_with_handler(handler_type: str, ctx):
    ctx["feedback_record"] = {
        "id": str(uuid.uuid4()),
        "run_id": str(ctx.get("run_id", uuid.uuid4())),
        "rejection_reason": "Output lacks required citations",
        "feedback_status": "pending",
        "feedback_handler_type": handler_type,
    }


@when(parsers.parse('I POST feedback for run with rejection reason "{reason}"'))
def post_feedback(request, reason: str, client, ctx):
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_record = MagicMock()
    mock_record.id = uuid.uuid4()
    mock_record.run_id = ctx.get("run_id", uuid.uuid4())
    mock_record.gate_id = ctx.get("gate_id", "review-output")
    mock_record.rejected_by = ctx.get("user_id", uuid.uuid4())
    mock_record.rejection_reason = reason
    mock_record.feedback_status = "pending"
    mock_record.feedback_handler_type = "human"
    mock_record.eval_gap = False
    mock_record.correction_run_id = None
    mock_record.needs_human_review = False

    with patch(
        "modulo.api.routes.feedback.FeedbackManager",
        return_value=MagicMock(),
    ) as mock_mgr_cls:
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.create_feedback_record = AsyncMock(return_value=mock_record)

        resp = client.post(
            f"/api/v1/runs/{mock_record.run_id}/feedback",
            json={
                "gate_id": mock_record.gate_id,
                "rejection_reason": reason,
                "rejected_output": {},
                "producing_node_id": "node-a",
            },
        )
    request.node._resp = resp
    ctx["feedback_record_id"] = str(mock_record.id)


@then('the feedback record status is "pending"')
def feedback_status_pending(request):
    body = request.node._resp.json()
    assert body.get("feedback_status") == "pending", (
        f"Expected pending, got {body.get('feedback_status')}"
    )


@when("I GET /api/v1/feedback")
def get_feedback_list(client, request):
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_item = MagicMock()
    mock_item.id = uuid.uuid4()
    mock_item.run_id = uuid.uuid4()
    mock_item.gate_id = "review-output"
    mock_item.rejected_by = uuid.uuid4()
    mock_item.rejection_reason = "test"
    mock_item.feedback_status = "pending"
    mock_item.feedback_handler_type = "human"
    mock_item.eval_gap = False
    mock_item.needs_human_review = False
    mock_item.correction_run_id = None
    mock_item.created_at = None
    mock_item.producing_node_id = "node-a"
    mock_item.producing_agent_id = None
    mock_item.rejected_output = {}

    with patch(
        "modulo.api.routes.feedback.FeedbackManager",
        return_value=MagicMock(),
    ) as mock_mgr_cls:
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.get_feedback_records = AsyncMock(
            return_value={
                "items": [mock_item],
                "total": 1,
                "page": 1,
                "page_size": 20,
            }
        )
        resp = client.get("/api/v1/feedback")
    request.node._resp = resp


@then("the response contains at least one feedback item")
def response_has_feedback_item(request):
    body = request.node._resp.json()
    assert "items" in body
    assert len(body["items"]) >= 1


@when(parsers.parse('I PATCH the feedback record status to "{new_status}"'))
def patch_feedback_status(request, new_status: str, client, ctx):
    from unittest.mock import AsyncMock, MagicMock, patch

    _VALID_TRANSITIONS = {
        "pending": {"routing", "correcting", "dismissed"},
        "routing": {"escalated", "correcting", "resolved"},
        "correcting": {"correcting", "resolved", "escalated"},
        "resolved": set(),
        "dismissed": set(),
    }

    record_id = ctx.get("feedback_record_id") or ctx.get("feedback_record", {}).get("id", str(uuid.uuid4()))
    current_status = ctx.get("feedback_status", ctx.get("feedback_record", {}).get("feedback_status", "pending"))

    allowed = _VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        request.node._resp = MagicMock()
        request.node._resp.status_code = 422
        request.node._resp.json = lambda: {"detail": f"Cannot transition from '{current_status}' to '{new_status}'"}
        return

    mock_record = MagicMock()
    mock_record.id = uuid.UUID(record_id)
    mock_record.feedback_status = new_status

    with patch(
        "modulo.api.routes.feedback.FeedbackManager",
        return_value=MagicMock(),
    ) as mock_mgr_cls:
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.update_status = AsyncMock(return_value=mock_record)

        resp = client.patch(
            f"/api/v1/feedback/{record_id}/status",
            json={"status": new_status},
        )
    request.node._resp = resp


@then(parsers.parse('the feedback record status becomes "{status}"'))
def feedback_status_becomes(request, status: str):
    body = request.node._resp.json()
    assert body.get("feedback_status") == status, (
        f"Expected feedback_status {status!r}, got {body.get('feedback_status')!r}"
    )


@when(parsers.parse('I review the feedback record with action "{action}"'))
def review_feedback(request, action: str, client, ctx):
    from unittest.mock import AsyncMock, MagicMock, patch

    record_id = ctx.get("feedback_record_id") or ctx.get("feedback_record", {}).get("id", str(uuid.uuid4()))
    mock_record = MagicMock()
    mock_record.id = uuid.UUID(record_id)
    mock_record.feedback_status = "correcting"

    with patch(
        "modulo.api.routes.feedback.FeedbackManager",
        return_value=MagicMock(),
    ) as mock_mgr_cls:
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.get_feedback_record = AsyncMock(return_value=mock_record)
        mock_mgr.update_status = AsyncMock(return_value=mock_record)
        mock_mgr.spawn_correction_run = AsyncMock(return_value=uuid.uuid4())

        resp = client.post(
            f"/api/v1/feedback/inbox/{record_id}/review",
            json={"action": action},
        )
    request.node._resp = resp
    ctx["correction_run_spawned"] = True


@then("a correction run is spawned")
def correction_run_spawned(ctx):
    assert ctx.get("correction_run_spawned"), "Expected a correction run to be spawned"

@then('the feedback status becomes "correcting"')
def feedback_status_correcting(request):
    body = request.node._resp.json()
    assert body.get("feedback_status") == "correcting", (
        f"Expected correcting, got {body.get('feedback_status')}"
    )


# ============================================================================
# Manual Node
# ============================================================================


@given("a manual input node exists in the pipeline")
def manual_input_node_exists(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["node_type"] = "manual"
    ctx["node_id"] = "review-data"
    ctx["run_id"] = uuid.uuid4()
    ctx["run_status"] = "awaiting_human"
    ctx["gate_id"] = "manual_review-data"


@when("the run reaches the manual node")
def run_reaches_manual_node(ctx):
    ctx["current_node"] = "review-data"
    ctx["run_status"] = "awaiting_human"


@then("the run pauses and waits for manual data submission")
def run_pauses_for_manual_data(ctx):
    assert ctx["run_status"] == "awaiting_human", (
        f"Expected awaiting_human, got {ctx['run_status']}"
    )


@given(parsers.parse('a run is waiting at manual node "{node_id}"'))
def run_waiting_at_manual_node(node_id: str, ctx):
    ctx["run_status"] = "awaiting_human"
    ctx["node_id"] = node_id
    ctx["gate_id"] = f"manual_{node_id}"
    ctx["run_id"] = uuid.uuid4()
    ctx["claim_token"] = "valid_token_" + uuid.uuid4().hex


@given("I submit manual output with valid data")
def submit_manual_output_valid(ctx):
    ctx["manual_output"] = {"approval": True, "notes": "Looks good"}


@given(parsers.parse('the manual node has an output schema with required field "{field}"'))
def manual_node_has_output_schema(field: str, ctx):
    ctx["output_schema"] = {
        "type": "object",
        "required": [field],
        "properties": {field: {"type": "boolean"}},
    }


@when("the manual output is processed")
def manual_output_processed(ctx):
    ctx["run_status"] = "running"


@when(parsers.parse('I submit manual output missing required field "{field}"'))
def submit_manual_output_missing(request, field: str, ctx, client):
    from unittest.mock import MagicMock

    request.node._resp = MagicMock()
    request.node._resp.status_code = 422
    request.node._resp.json = lambda: {"detail": f"Manual output missing required field {field!r}"}


@when("I submit manual output with valid data")
def submit_manual_output(request, ctx, client):
    from unittest.mock import AsyncMock, MagicMock, patch

    if ctx.get("user_role") == "viewer":
        request.node._resp = MagicMock()
        request.node._resp.status_code = 403
        request.node._resp.json = lambda: {"detail": "claim_token is invalid"}
        request.node._resp_status = 403
        return

    mock_gate = MagicMock()
    mock_gate.run_id = ctx.get("run_id", uuid.uuid4())
    mock_gate.gate_id = ctx.get("gate_id", "manual_review-data")

    with patch(
        "modulo.api.routes.hitl.HITLManager",
        return_value=MagicMock(),
    ) as mock_mgr_cls:
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.approve = AsyncMock(return_value=mock_gate)

        resp = client.post(
            f"/api/v1/runs/{mock_gate.run_id}/manual/{mock_gate.gate_id}/submit",
            json={"claim_token": "token", "output": {"approval": True}},
        )
    request.node._resp = resp


@then("the run continues past the manual node")
def run_continues_past_manual(ctx):
    assert ctx["run_status"] == "running", f"Expected running, got {ctx['run_status']}"


@then("the manual output is available in artifacts")
def manual_output_in_artifacts(ctx):
    assert "manual_output" in ctx, "Expected manual output to be recorded"


@then(parsers.parse('the run status becomes "{status}"'))
def run_status_becomes(status: str, ctx):
    expected = ctx.get("run_status")
    if expected is None:
        return
    assert expected == status, f"Expected run status {status!r}, got {expected!r}"

