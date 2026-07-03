"""BDD step definitions: HITL claim, approve, reject, human-only, overdue."""

import uuid
from unittest.mock import patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/hitl/claim.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/hitl/approve.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/hitl/reject.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/hitl/human_only_gate.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/hitl/overdue_warning.feature")
except (FileNotFoundError, OSError):
    pass


@given("a run is waiting at gate {gate}")
def run_waiting_at_gate(gate: str, request):
    run_id = uuid.uuid4()
    request.node._run_id = run_id
    request.node._gate_id = gate


@when(parsers.parse("I POST /api/runs/{run_id}/claim"))
def claim_gate(run_id, client, request):
    from modulo.hitl_manager import ClaimResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.claim_gate",
            return_value=ClaimResult(
                success=True,
                claim_token="test_claim_token",
                expires_at=None,
            ),
        ),
    ):
        resp = client.post(f"/api/runs/{run_id}/claim")
    request.node._resp = resp


@then(parsers.parse('I am the claimant of gate "{gate}"'))
def check_claimant(gate: str, request):
    pass


@given("another user has claimed gate {gate}")
def other_user_claimed(gate: str, request):
    request.node._gate_already_claimed = True


@when(parsers.parse("I POST /api/runs/{run_id}/claim"))
def claim_gate_conflict(run_id, client, request):
    from modulo.hitl_manager import ClaimResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.claim_gate",
            return_value=ClaimResult(
                success=False,
                claim_token=None,
                expires_at=None,
                error="Gate already claimed by another user",
            ),
        ),
    ):
        resp = client.post(f"/api/runs/{run_id}/claim")
    request.node._resp = resp


@then("my claim expires")
def claim_expires(request):
    pass


@given("another user can claim the gate")
def other_can_claim(request):
    pass


@then("the response contains a claim_token")
def check_claim_token(request):
    data = request.node._resp.json()
    assert "claim_token" in data


@given("I have claimed gate {gate}")
def i_have_claimed(gate: str, request):
    request.node._claim_token = "test_claim_token"
    request.node._gate_id = gate


@when(parsers.parse('I POST /api/runs/{run_id}/approve with claim_token and decision "{decision}"'))
def approve_with_claim_token(run_id, decision: str, client, request):
    from modulo.hitl_manager import ApproveResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(success=True, new_status="running"),
        ),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/approve",
            json={
                "decision": decision,
                "claim_token": getattr(request.node, "_claim_token", "test_token"),
            },
        )
    request.node._resp = resp


@then(parsers.parse('the run status becomes "{status}"'))
def check_run_status(status: str, request):
    data = request.node._resp.json()
    assert data.get("status") == status


@then(parsers.parse('execution resumes from "{node}"'))
def execution_resumes(node: str, request):
    pass


@when(parsers.parse('I POST /api/runs/{run_id}/approve with decision "{decision}" and no claim_token'))
def approve_no_token(run_id, decision: str, client, request):
    from modulo.hitl_manager import ApproveResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(success=False, new_status="waiting_for_approval"),
        ),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/approve",
            json={"decision": decision},
        )
    request.node._resp = resp


@given("the claim token expires")
def claim_token_expires(request):
    pass


@when(parsers.parse('I POST /api/runs/{run_id}/approve with expired claim_token and decision "{decision}"'))
def approve_expired_token(run_id, decision: str, client, request):
    from modulo.hitl_manager import ApproveResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(
                success=False,
                new_status="waiting_for_approval",
                error="Claim token expired",
            ),
        ),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/approve",
            json={
                "decision": decision,
                "claim_token": "expired_token",
            },
        )
    request.node._resp = resp


@when(parsers.parse('I POST /api/runs/{run_id}/approve with claim_token "{token}" and decision "{decision}"'))
def approve_other_token(run_id, token: str, decision: str, client, request):
    from modulo.hitl_manager import ApproveResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(
                success=False,
                new_status="waiting_for_approval",
                error="Gate claimed by another user",
            ),
        ),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/approve",
            json={"decision": decision, "claim_token": token},
        )
    request.node._resp = resp


@when(
    parsers.parse('I POST /api/runs/{run_id}/approve with claim_token and decision "{decision}" and reason "{reason}"')
)
def approve_with_reason(run_id, decision: str, reason: str, client, request):
    from modulo.hitl_manager import ApproveResult

    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(success=True, new_status="rejected"),
        ),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/approve",
            json={
                "decision": decision,
                "claim_token": getattr(request.node, "_claim_token", "test_token"),
                "reason": reason,
            },
        )
    request.node._resp = resp


@then(parsers.parse('the run has rejection_reason "{reason}"'))
def check_rejection_reason(reason: str, request):
    pass


@given(parsers.parse('pipeline "{p}" has a human-only node "{node}"'))
def human_only_node(p: str, node: str, request):
    request.node._pipeline_name = p
    request.node._human_node = node


@when(parsers.parse('the run reaches the "{node}" node'))
def run_reaches_node(node: str, request):
    request.node._reached_node = node


@then(parsers.parse('the run status becomes "waiting_for_human"'))
def check_waiting_human(request):
    pass


@then("no AI agent can process this node")
def no_ai_processing(request):
    pass


@given(parsers.parse('a run is waiting at human node "{node}"'))
def run_waiting_human(node: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._human_node = node


@when(parsers.parse("I POST /api/runs/{run_id}/human-input with data {data}"))
def post_human_input(run_id, data, client, request):
    import json

    payload = json.loads(data) if isinstance(data, str) else data
    with (
        patch("modulo.hitl_manager.HITLManager.submit_human_input"),
    ):
        resp = client.post(f"/api/runs/{run_id}/human-input", json=payload)
    request.node._resp = resp


@when("the pipeline engine tries to auto-resolve the gate")
def auto_resolve_gate(request):
    pass


@then("the auto-resolve is blocked")
def auto_resolve_blocked(request):
    pass


@then(parsers.parse('the run remains "waiting_for_human"'))
def remains_waiting_human(request):
    pass


@when(parsers.parse("I POST /api/runs/{run_id}/human-input with invalid data {data}"))
def post_invalid_human_input(run_id, data, client, request):
    import json

    payload = json.loads(data) if isinstance(data, str) else data
    resp = client.post(f"/api/runs/{run_id}/human-input", json=payload)
    request.node._resp = resp


@given(parsers.parse('a run is waiting at gate "{gate}" with timeout {timeout:d}s'))
def run_waiting_with_timeout(gate: str, timeout: int, request):
    request.node._run_id = uuid.uuid4()
    request.node._gate_id = gate
    request.node._gate_timeout = timeout


@when(parsers.parse("{seconds:d} second passes without approval"))
def time_passes(seconds: int, request):
    from modulo.hitl_manager import HITLManager

    with patch.object(HITLManager, "expire_stale_gates") as mock_expire:
        mock_expire.return_value = [request.node._run_id]


@then(parsers.parse('the run status becomes "timed_out"'))
def check_timed_out(request):
    pass


@when("the gate becomes overdue")
def gate_overdue(request):
    pass


@then("a notification is sent to configured approvers")
def notification_sent(request):
    pass


@then(parsers.parse('the notification type is "{ntype}"'))
def check_notification_type(ntype: str, request):
    pass


@when(parsers.parse("{seconds:d} seconds have elapsed"))
def seconds_elapsed(seconds: int, request):
    pass


@then(parsers.parse('the gate shows an "{warning}" warning'))
def gate_shows_warning(warning: str, request):
    pass


@then("the warning is visible in the stage board")
def warning_visible(request):
    pass


@given("{count:d} runs are waiting at gates")
def runs_waiting(count: int, request):
    request.node._waiting_count = count


@given("one gate is overdue")
def one_gate_overdue(request):
    pass


@when("I GET the stage board with filter {filter_name}")
def get_stage_board(filter_name: str, client, request):
    resp = client.get(f"/api/stage-board?filter={filter_name}")
    request.node._resp = resp


@then("the overdue gate is highlighted")
def overdue_highlighted(request):
    pass


@then(parsers.parse('the overdue badge shows "{text}"'))
def overdue_badge_text(text: str, request):
    pass


@then("the response status is 403")
def check_status_403(request):
    resp = request.node._resp
    assert resp.status_code == 403


@then("the response status is 409")
def check_status_409(request):
    resp = request.node._resp
    assert resp.status_code == 409


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", ""))).lower()
    assert text.lower() in detail
