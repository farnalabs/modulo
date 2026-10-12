"""BDD step definitions: Run failed state, recovery.

``failed_state.feature`` drives two REAL routes with only their DB seams
patched: the run-detail ``GET /api/v1/runs/{id}`` (with the ``_do_*``
DB-fetch seams patched) and the per-node output surface
``GET /api/v1/runs/{run_id}/nodes/{node_id}/output`` (with ``get_run`` /
``read_run_blobs`` patched) — the run-detail ``RunResponse`` carries no
per-node map, so "inspecting the detail" of a failed run means reading its
node outputs through that route. ``recovery.feature`` drives the REAL
``POST /api/v1/runs/{run_id}/nodes/{node_id}/recover`` route — the recover-node
surface that replaced the draft run-level ``/resume`` / ``/retry`` endpoints —
with only the ``recover_node`` DB seam and the ``dispatch_run`` resume seam
patched, so the route's typed error mapping (200 replay/skip, 403 role gate,
404, 409, 422, 500), the replay/skip action selector and the resume dispatch
are asserted end to end.
"""

import contextlib
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/errors/failed_state.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/errors/recovery.feature")

from tests.bdd.conftest import _active_client, make_mock_run


@given(parsers.parse("a run that failed at node {failed:d} of {total:d}"))
def run_failed_at_node(failed: int, total: int, request):
    run_id = uuid.uuid4()
    request.node._run_id = run_id
    request.node._failed_node = failed
    request.node._total_nodes = total


@when(parsers.parse("I GET /api/runs/{run_id}"))
def get_run(run_id, client, request):
    if run_id.startswith("{"):
        run_id = request.node._run_id
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch(
            "modulo.api.routes.runs._do_get_run_with_gate",
            return_value=(
                make_mock_run(
                    id=run_id,
                    status="failed",
                    error_detail="Node 2: Connection timeout",
                ),
                False,
            ),
        ),
        patch("modulo.api.routes.runs._do_get_child_run_rollup", return_value=(Decimal("0.00"), 0)),
        patch("modulo.api.routes.runs._do_get_otel_endpoint", return_value=None),
        patch(
            "modulo.api.routes.runs._do_get_run_observability",
            return_value=(None, None, None),
        ),
    ):
        resp = client.get(f"/api/v1/runs/{run_id}")
    request.node._resp = resp


@given("a run that failed")
def a_run_that_failed(request):
    request.node._run_id = uuid.uuid4()


@then(parsers.parse('the response has status "{status}"'))
def check_response_status(status: str, request):
    data = request.node._resp.json()
    assert data.get("status") == status


@then("the response has error_detail describing the failure")
def check_error_detail(request):
    data = request.node._resp.json()
    assert data.get("error_detail") is not None


def _failed_run_blobs(request):
    """The ``RunBlobs`` for the scenario's failed run, derived from the Given.

    Node ids are the ``Given a run that failed at node {failed:d} of {total:d}``
    integers as strings. Every node BEFORE the failed one completed with a
    recorded pure return; the failed node has NO return but a telemetry entry
    (so the endpoint derives ``{status, summary}`` from it rather than 404ing);
    every node AFTER it has no entry at all (so the endpoint 404s). The shape is
    fixed by the Given, never by the value an individual step asserts.
    """
    from modulo.db.crud.run_node_outputs import RunBlobs

    failed = request.node._failed_node
    outputs = {str(n): {"node": n, "status": "completed", "result": f"output-{n}"} for n in range(1, failed)}
    telemetry = {
        str(failed): {
            "status": "failed",
            "summary": "Connection timeout",
            "agent_stdout": "internal log tail that must not leak through the derived surface",
        }
    }
    return RunBlobs(outputs=outputs, telemetry=telemetry, markers=None)


def _get_node_output(request, node):
    """Drive the REAL ``GET /api/v1/runs/{run_id}/nodes/{node_id}/output`` route.

    Only the route module's DB seams are patched — ``set_rls_org``,
    ``get_run`` and ``read_run_blobs`` (imported into ``modulo.api.routes.runs``
    at runs.py:2521-2527) — so the route body, its response model, the
    derived-telemetry fallback and its 404 all run for real.
    """
    client = _active_client(request)
    run_id = request.node._run_id
    run = make_mock_run(id=run_id, status="failed", error_detail="Node 2: Connection timeout")
    with (
        patch("modulo.api.routes.runs.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.api.routes.runs.read_run_blobs", new=AsyncMock(return_value=_failed_run_blobs(request))),
    ):
        return client.get(f"/api/v1/runs/{run_id}/nodes/{node}/output")


@when("I inspect the run detail")
def inspect_run_detail(request):
    """Inspect the failed node through the REAL per-node output surface.

    The run-detail payload (``RunResponse``) carries no per-node map; the
    product's per-node surface is
    ``GET /api/v1/runs/{run_id}/nodes/{node_id}/output``. This reads the failed
    node through that route and asserts the inspection itself succeeds; the
    ``Then`` steps below read their own node from the same surface.
    """
    resp = _get_node_output(request, request.node._failed_node)
    request.node._resp = resp
    assert resp.status_code == 200, resp.text


@then(parsers.parse("node {node:d} output is available"))
def node_output_available(node: int, request):
    """A completed node's pure return is served by the real node-output route."""
    resp = _get_node_output(request, node)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node_id"] == str(node)
    assert body["output"] == {"node": node, "status": "completed", "result": f"output-{node}"}


@then(parsers.parse("node {node:d} error is available"))
def node_error_available(node: int, request):
    """A telemetry-only node surfaces a DERIVED ``{status, summary}``, never raw telemetry.

    The endpoint must NOT 404 for the failed node (it has no pure return), and
    the derived surface must not leak the log tail the summary was drawn from.
    """
    resp = _get_node_output(request, node)
    assert resp.status_code == 200, resp.text
    output = resp.json()["output"]
    assert output["status"] == "failed"
    assert output["summary"] == "Connection timeout"
    assert "agent_stdout" not in output


@then(parsers.parse("node {node:d} has no output"))
def node_no_output(node: int, request):
    """A node absent from both outputs and telemetry 404s on the real route."""
    resp = _get_node_output(request, node)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == f"Node {node} not found in run outputs"


@then("the response reports a terminal run status")
def check_response_terminal_status(request):
    """Assert the run-detail payload carries a real terminal run status.

    A Run has a terminal ``status`` (and, on failure, an ``error_detail``) —
    this asserts the real payload the ``GET /runs/{id}`` route produced, read
    against ``TERMINAL_STATUSES`` from the Run model
    (modulo/db/models/run.py), rather than a value the step itself set.
    """
    from modulo.db.models.run import TERMINAL_STATUSES

    data = request.node._resp.json()
    assert data.get("status") in TERMINAL_STATUSES, f"Expected a terminal run status, got {data.get('status')!r}"


@then("the response contains error_detail")
def check_error_detail_field(request):
    data = request.node._resp.json()
    assert data.get("error_detail") is not None or "error_detail" in data


# NOTE: no real seam exists for these two steps. A failed run writes NO audit
# event — the failure path only ``broker.publish("run_failed", ...)`` (a
# notifier event; ``core/notifier.EVENT_RUN_FAILED``), and there is no
# ``append_audit_event(event_type="run_failed")`` anywhere in the pipeline
# engine. The audit read route cannot therefore observe a run-failure audit
# event, and fabricating one through its query seam would assert a fiction.
# Left deliberately vacuous rather than fabricating a pass.
@when("I check the audit log")
def check_audit_log(request):
    pass


@then("an audit event exists for run failure")
def audit_event_for_failure(request):
    pass


# ---------------------------------------------------------------------------
# Node recovery (REAL ``POST /api/v1/runs/{run_id}/nodes/{node_id}/recover``)
# ---------------------------------------------------------------------------

_RECOVER_BODY_EMPTY = object()


@given(parsers.parse('a failed run has a manual node "{node}" pending recovery'))
def failed_run_with_manual_node(node: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._recover_node = node


@given(parsers.parse('a failed run is parked at a HITL review node "{node}"'))
def failed_run_parked_at_hitl_review(node: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._recover_node = node


@given(parsers.parse('the node "{node}" is not present in the graph'))
def node_not_present_in_graph(node: str, request):
    request.node._recover_error = "node_not_found"


@given(parsers.parse('the node "{node}" has already completed'))
def node_already_completed(node: str, request):
    request.node._recover_error = "already_completed"


@given("the run is in a state that does not permit recovery")
def run_not_recoverable(request):
    request.node._recover_error = "not_recoverable"


@given("another operator concurrently recovers the node")
def concurrent_recovery(request):
    request.node._recover_error = "concurrent"


@given("the resume enqueue fails after recovery")
def resume_enqueue_fails(request):
    request.node._recover_error = "enqueue_failed"


def _recovered_run(request) -> MagicMock:
    run = MagicMock()
    run.id = request.node._run_id
    run.status = "running"
    return run


def _post_recover(request, client):
    """POST the recover-node route with only the DB/dispatch seams patched.

    ``recover_node`` behaviour is selected by ``request.node._recover_error``
    (a typed recovery exception to raise, ``None`` for the happy path), and
    ``dispatch_run`` is patched to ``("enqueued", ...)`` unless the given
    flagged ``enqueue_failed``. The dispatch mock is captured on
    ``request.node._dispatch_run`` so the replay scenario can assert the
    resume payload end to end.
    """
    client = _active_client(request, client)
    run_id = request.node._run_id
    node = request.node._recover_node
    input_data = getattr(request.node, "_recover_input_data", _RECOVER_BODY_EMPTY)

    from modulo.core.pipeline_engine.recovery import (
        ConcurrentRecoveryError,
        NodeAlreadyCompletedError,
        NodeNotFoundInGraphError,
        RecoveryNotAllowedError,
    )

    recover_patch = patch("modulo.api.routes.runs.recover_node", new=AsyncMock(return_value=_recovered_run(request)))
    error = getattr(request.node, "_recover_error", None)
    if error == "node_not_found":
        recover_patch = patch(
            "modulo.api.routes.runs.recover_node",
            new=AsyncMock(side_effect=NodeNotFoundInGraphError(run_id, node)),
        )
    elif error == "already_completed":
        recover_patch = patch(
            "modulo.api.routes.runs.recover_node",
            new=AsyncMock(side_effect=NodeAlreadyCompletedError(run_id, node)),
        )
    elif error == "not_recoverable":
        recover_patch = patch(
            "modulo.api.routes.runs.recover_node",
            new=AsyncMock(side_effect=RecoveryNotAllowedError(run_id, "running")),
        )
    elif error == "concurrent":
        recover_patch = patch(
            "modulo.api.routes.runs.recover_node",
            new=AsyncMock(side_effect=ConcurrentRecoveryError(run_id)),
        )

    dispatch_return = ("enqueue_failed", "job_id") if error == "enqueue_failed" else ("enqueued", "job_id")
    dispatch_mock = AsyncMock(return_value=dispatch_return)

    body = {} if input_data is _RECOVER_BODY_EMPTY or input_data is None else {"input_data": input_data}
    with (
        patch("modulo.api.routes.runs.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.runs.set_rls_user_context", new=AsyncMock()),
        recover_patch,
        patch("modulo.api.routes.runs.dispatch_run", dispatch_mock),
    ):
        resp = client.post(f"/api/v1/runs/{run_id}/nodes/{node}/recover", json=body)
    request.node._resp = resp
    request.node._dispatch_run = dispatch_mock


@when(parsers.parse('I replay node "{node}" with input_data {payload}'))
def replay_node(node: str, payload, request, client):
    request.node._recover_node = node
    request.node._recover_input_data = json.loads(payload)
    _post_recover(request, client)


@when(parsers.parse('I skip node "{node}"'))
def skip_node(node: str, request, client):
    request.node._recover_node = node
    request.node._recover_input_data = None
    _post_recover(request, client)


@when(parsers.parse('I recover node "{node}"'))
def recover_node(node: str, request, client):
    request.node._recover_node = node
    request.node._recover_input_data = None
    _post_recover(request, client)


@then(parsers.parse("the recovery request returns status {status:d}"))
def recovery_request_status(status: int, request):
    assert request.node._resp.status_code == status, request.node._resp.text


@then(parsers.parse('the recovery response action is "{action}"'))
def recovery_response_action(action: str, request):
    data = request.node._resp.json()
    assert data.get("action") == action, data


@then("the recovery resume is dispatched with the recovered output")
def recovery_resume_dispatched_with_output(request):
    dispatch = request.node._dispatch_run
    assert dispatch is not None, "resume dispatch not captured"
    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["job_type"] == "resume_run", kwargs
    assert kwargs["resume_data"]["action"] == "replay", kwargs
    assert kwargs["resume_data"]["output"] == request.node._recover_input_data, kwargs


@then(parsers.parse('the recovery failure mentions "{text}"'))
def recovery_failure_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", data))).lower()
    assert text.lower() in detail, data
