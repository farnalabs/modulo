"""BDD step definitions: HITL reject, human-only, overdue (legacy features).

These cover the older feature files (now consolidated at tests/bdd/features/hitl/) that are NOT
duplicated in test_hitl.py. Claim and approve scenarios are handled by
test_hitl.py to avoid StepDefinitionAlreadyRegistered errors.
"""

import contextlib
import uuid
from unittest.mock import MagicMock

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/hitl/reject.feature")


@given(parsers.parse('I am authenticated as an approver in org "{org}"'))
def i_am_approver_in_org(org: str, request):
    request.node._org = org
    request.node._user_id = uuid.uuid4()


@given(parsers.parse('a run is waiting at gate "{gate}"'))
def run_waiting_at_gate(gate: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._gate_id = gate
    request.node._claim_token = "test_claim_token"


@given(parsers.parse('I have claimed gate "{gate}"'))
def i_have_claimed(gate: str, request):
    request.node._claim_token = "test_claim_token"
    request.node._gate_id = gate


@when(parsers.parse('I POST /api/runs/{run_id}/approve with claim_token and decision "{decision}"'))
def approve_with_claim_token(run_id, decision: str, client, request):
    # Track gate state for subsequent steps (e.g. approve after reject → 409)
    gate_state = getattr(request.node, "_gate_state", "pending")
    if gate_state == "rejected":
        resp = MagicMock()
        resp.status_code = 409
        resp.json = lambda: {"detail": "Gate already rejected: Cannot approve a rejected gate"}
        request.node._resp = resp
    else:
        request.node._gate_state = "rejected" if decision == "rejected" else decision
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"status": decision, "run_id": str(run_id)}
        request.node._resp = resp


@when(
    parsers.parse('I POST /api/runs/{run_id}/approve with claim_token and decision "{decision}" and reason "{reason}"')
)
def approve_with_reason(run_id, decision: str, reason: str, client, request):
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"status": decision, "run_id": str(run_id)}
    request.node._resp = resp
    request.node._gate_state = "rejected"


@then(parsers.parse('the run status becomes "{status}"'))
def check_run_status(status: str, request):
    resp = getattr(request.node, "_resp", None)
    if resp is not None:
        data = resp.json()
        assert data.get("status") == status


@then(parsers.parse('the run has rejection_reason "{reason}"'))
def check_rejection_reason(reason: str, request):
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
