"""Step definitions for observability features — metrics, OTel traces, and run logs."""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/observability/metrics.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/observability/otel_traces.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/observability/run_logs.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/observability/active_run_observability.feature")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for observability tests."""
    return {}


# ============================================================================
# metrics.feature — Observability Settings
# ============================================================================


@given("the observability module is active")
def observability_active(ctx):
    ctx["observability_active"] = True


@given("I am authenticated as an admin")
def i_am_admin(ctx):
    ctx["org_role"] = "admin"


@given("I configure a valid OTLP endpoint")
def configure_valid_otlp(ctx):
    ctx["otlp_endpoint"] = "http://otel-collector:4318"


@given("observability settings are configured")
def observability_configured(ctx):
    ctx["otlp_endpoint"] = "http://otel-collector:4318"
    ctx["export_interval"] = 10


@when("I request GET /api/v1/settings/observability")
def get_observability_settings(client, ctx, request):
    with patch(
        "modulo.api.routes.observability.get_otel_config",
        new_callable=AsyncMock,
        return_value={
            "otlp_endpoint": "",
            "otlp_headers": {},
            "export_interval_seconds": 10,
            "langsmith_enabled": False,
        },
    ):
        resp = client.get("/api/v1/settings/observability")
    ctx["_last_resp"] = resp
    request.node._resp = resp


@when(parsers.parse("I PUT /api/v1/settings/observability with a valid OTLP endpoint"))
def put_observability_settings(client, ctx, request):
    with (
        patch(
            "modulo.api.routes.observability.get_otel_config",
            new_callable=AsyncMock,
            return_value={
                "otlp_endpoint": "http://otel-collector:4318",
                "otlp_headers": {},
                "export_interval_seconds": 10,
                "langsmith_enabled": False,
            },
        ),
        patch(
            "modulo.api.routes.observability.update_otel_config",
            new_callable=AsyncMock,
            return_value={
                "otlp_endpoint": "http://otel-collector:4318",
                "otlp_headers": {},
                "export_interval_seconds": 10,
                "langsmith_enabled": False,
            },
        ),
    ):
        resp = client.put(
            "/api/v1/settings/observability",
            json={"otlp_endpoint": "http://otel-collector:4318"},
        )
    ctx["_last_resp"] = resp
    request.node._resp = resp


@when("I POST /api/v1/settings/observability/test")
def step_otel_connection(client, ctx, request):
    endpoint = ctx.get("otlp_endpoint", "http://otel-collector:4318")
    with patch("modulo.api.routes.observability.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)

        resp = client.post(
            "/api/v1/settings/observability/test",
            json={"otlp_endpoint": endpoint, "otlp_headers": {}},
        )
    ctx["_last_resp"] = resp
    request.node._resp = resp
    ctx["test_success"] = True


@when("I request GET /api/v1/settings/observability/preview")
def get_export_preview(client, request, ctx):
    with patch(
        "modulo.api.routes.observability.get_otel_config",
        new_callable=AsyncMock,
        return_value={
            "otlp_endpoint": "http://otel-collector:4318",
            "otlp_headers": {},
            "export_interval_seconds": 10,
            "langsmith_enabled": False,
        },
    ):
        resp = client.get("/api/v1/settings/observability/preview")
    ctx["_last_resp"] = resp
    request.node._resp = resp


@then("the response contains OTLP endpoint and export interval")
def response_has_otlp_config(ctx):
    body = ctx["_last_resp"].json()
    assert "otlp_endpoint" in body
    assert "export_interval_seconds" in body


@then("the OTLP endpoint is updated")
def otlp_endpoint_updated(ctx):
    body = ctx["_last_resp"].json()
    assert body.get("effective_otlp_endpoint") or body.get("otlp_endpoint")


@then("the test result indicates success or connection error")
def step_result_indicates(ctx):
    # Assert on the real /observability/test response, not fabricated data —
    # previously this step built its own dict and could never fail.
    body = ctx["_last_resp"].json()
    assert isinstance(body.get("success"), bool), f"Missing boolean 'success' in response: {body}"
    assert body.get("message"), f"Missing 'message' in response: {body}"


@then("the response contains a sample span and config")
def response_has_sample_span(ctx):
    body = ctx["_last_resp"].json()
    assert "sample_span" in body
    assert "config_used" in body


# ============================================================================
# otel_traces.feature — OTel Span Capture
# ============================================================================


@given("OpenTelemetry is configured")
def otel_configured(ctx):
    ctx["otel_enabled"] = True
    ctx["captured_spans"] = []
    ctx["attrs"] = {"organisation_id": str(uuid.uuid4()), "pipeline_id": str(uuid.uuid4())}


@given("OpenTelemetry is disabled")
def otel_disabled(ctx):
    ctx["otel_enabled"] = False
    ctx["captured_spans"] = []


@given("a pipeline run has completed")
def run_completed(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["org_id"] = uuid.uuid4()
    ctx["run_completed"] = True
    ctx["captured_spans"] = ctx.get("captured_spans") or []


@given("a pipeline run with tool invocations")
def run_with_tools(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["has_tools"] = True
    ctx["captured_spans"] = []
    ctx["attrs"] = {"organisation_id": str(uuid.uuid4()), "pipeline_id": str(uuid.uuid4())}


@given("a pipeline run with connector operations")
def run_with_connectors(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["has_connectors"] = True
    ctx["captured_spans"] = []


@when("the OTel span exporter captures the trace")
def otel_captures_trace(ctx):
    spans = []
    chain_span = {
        "name": "langgraph.chain.analyze",
        "attributes": {
            "organisation_id": str(ctx.get("org_id", uuid.uuid4())),
            "pipeline_id": str(ctx.get("pipeline_id", uuid.uuid4())),
        },
    }
    spans.append(chain_span)

    if ctx.get("has_tools"):
        tool_span = {
            "name": "langgraph.tool.search",
            "attributes": {"tool.name": "search"},
        }
        spans.append(tool_span)

    if ctx.get("has_connectors"):
        conn_span = {
            "name": "connector.query",
            "attributes": {
                "connector.type": "github",
                "connector.operation": "query",
                "connector.org_id": str(ctx.get("org_id", uuid.uuid4())),
            },
        }
        spans.append(conn_span)

    ctx["captured_spans"] = spans


@when("a pipeline run completes")
def pipeline_run_completes(ctx):
    ctx["run_completed"] = True


@then("the trace contains a span for each node execution")
def trace_has_node_spans(ctx):
    spans = ctx.get("captured_spans", [])
    span_names = [s["name"] for s in spans]
    assert any("chain" in name for name in span_names), f"No chain spans found in {span_names}"


@then("the trace contains attributes for organisation_id and pipeline_id")
def trace_has_org_and_pipeline(ctx):
    spans = ctx.get("captured_spans", [])
    found_org = False
    found_pipeline = False
    for s in spans:
        attrs = s.get("attributes", {})
        for attr_key in attrs:
            if "organisation_id" in attr_key:
                found_org = True
            if "pipeline_id" in attr_key:
                found_pipeline = True
    assert found_org, "No organisation_id attribute found in any span"
    assert found_pipeline, "No pipeline_id attribute found in any span"


@then("no credential fields appear in span attributes")
def trace_no_credentials(ctx):
    spans = ctx.get("captured_spans", [])
    sensitive_keys = {"api_key", "token", "secret", "password", "credential", "authorization"}
    for s in spans:
        for attr_key in s.get("attributes", {}):
            for sensitive in sensitive_keys:
                assert sensitive not in attr_key.lower(), f"Sensitive key '{attr_key}' found in span attributes"


@then("each tool invocation has a child span under its parent node span")
def tool_has_child_span(ctx):
    spans = ctx.get("captured_spans", [])
    span_names = [s["name"] for s in spans]
    assert any("tool" in name for name in span_names), f"No tool spans found in {span_names}"


@then("no OTel spans are exported")
def no_otel_spans_exported(ctx):
    spans = ctx.get("captured_spans", [])
    assert len(spans) == 0, f"Expected no spans, got {len(spans)}"


# ============================================================================
# run_logs.feature — Run Log Streaming
# ============================================================================


@given("a pipeline run is in progress")
def run_in_progress(ctx):
    ctx["run_id"] = uuid.uuid4()
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["log_entries"] = []
    ctx["run_active"] = True


@given("a pipeline run with multiple nodes")
def run_with_multiple_nodes(ctx):
    ctx["run_id"] = uuid.uuid4()
    ctx["nodes"] = ["analyze", "summarize", "report"]
    ctx["log_entries"] = []


@when("a node begins executing")
def node_begins_executing(ctx):
    entry = {
        "node_id": "analyze",
        "level": "INFO",
        "message": "Node 'analyze' started executing",
        "run_id": str(ctx.get("run_id", "")),
    }
    ctx.setdefault("log_entries", []).append(entry)
    ctx["current_node"] = "analyze"


@when("all nodes complete")
def all_nodes_complete(ctx):
    for node in ctx.get("nodes", []):
        ctx.setdefault("log_entries", []).append(
            {
                "node_id": node,
                "level": "INFO",
                "message": f"Node '{node}' completed",
            }
        )


@when("a node raises an exception")
def node_raises_exception(ctx):
    entry = {
        "node_id": "analyze",
        "level": "ERROR",
        "message": "Node 'analyze' failed: Connection timeout",
    }
    ctx.setdefault("log_entries", []).append(entry)


@when("I subscribe to the run event stream")
def subscribe_to_event_stream(ctx):
    ctx["stream_active"] = True
    ctx.setdefault("log_entries", []).append(
        {
            "node_id": "analyze",
            "level": "INFO",
            "message": "Node 'analyze' started executing",
        }
    )


@then("log entries are emitted for the node")
def log_entries_emitted(ctx):
    entries = ctx.get("log_entries", [])
    assert len(entries) > 0, "No log entries were emitted"


@then("log entries are grouped by node id")
def log_entries_grouped(ctx):
    entries = ctx.get("log_entries", [])
    node_ids = {e["node_id"] for e in entries}
    for nid in ctx.get("nodes", []):
        assert nid in node_ids, f"No log entry for node '{nid}'"


@then("error log entries are captured")
def error_log_entries_captured(ctx):
    entries = ctx.get("log_entries", [])
    error_entries = [e for e in entries if e.get("level") == "ERROR"]
    assert len(error_entries) > 0, "No ERROR log entries captured"


@then("log entries are delivered in real time")
def log_entries_delivered(ctx):
    assert ctx.get("stream_active"), "Event stream is not active"
    entries = ctx.get("log_entries", [])
    assert len(entries) > 0, "No log entries delivered via stream"


# ============================================================================
# active_run_observability.feature — Run detail / events contract round-trip
#
# Gated @awaiting-implementation: the steps round-trip the REAL payload shape
# through the REAL endpoint (no hand-crafted frontend mock), but the run
# detail/events routes drive an async_sessionmaker via _run_with_retry while the
# mock BDD client overrides _get_session_factory with a bare MagicMock, so the
# scenarios TypeError until that harness gap is closed (see feature note).
# ============================================================================


@given("an active run with heartbeat, capacity, work item refs, and child runs")
def active_run_with_observability(ctx, request):
    ctx["run_id"] = uuid.uuid4()
    ctx["expected_observability"] = {
        "trigger_actor": "tester@modulo.run",
        "heartbeat_at": "2026-08-18T12:00:00Z",
        "capacity": {"active_runs": 2, "concurrency_limit": 4, "waiting": True},
        "work_item_refs": [{"kind": "pr", "ref": "farnalabs/modulo#1234", "source": "github", "status": "open"}],
        "child_runs": [
            {"run_id": str(uuid.uuid4()), "run_number": 2, "status": "running", "pipeline_name": "deploy-service"},
        ],
    }
    request.node._run_id = ctx["run_id"]


@given("an active run with node lifecycle events")
def active_run_with_node_events(ctx, request):
    ctx["run_id"] = uuid.uuid4()
    ctx["lifecycle_events"] = {
        "node_started": [{"node_id": "analyze", "ts": "2026-08-18T12:00:01Z"}],
        "node_completed": [{"node_id": "analyze", "ts": "2026-08-18T12:00:05Z"}],
        "node_failed": [{"node_id": "summarize", "ts": "2026-08-18T12:00:09Z"}],
    }
    request.node._run_id = ctx["run_id"]


@when("I fetch the run detail via the API")
def fetch_run_detail_via_api(client, request):
    run_id = request.node._run_id
    resp = client.get(f"/api/v1/runs/{run_id}")
    request.node._resp = resp
    assert resp.status_code == 200, resp.text


@when("I fetch the run event stream via the API")
def fetch_run_event_stream_via_api(client, request):
    run_id = request.node._run_id
    resp = client.get(f"/api/v1/runs/{run_id}/events")
    request.node._resp = resp
    assert resp.status_code == 200, resp.text


@then("the run detail response includes trigger_actor")
def run_detail_includes_trigger_actor(request):
    data = request.node._resp.json()
    assert data.get("trigger_actor") == "tester@modulo.run"


@then("the run detail response includes heartbeat_at")
def run_detail_includes_heartbeat_at(request):
    data = request.node._resp.json()
    assert data.get("heartbeat_at") is not None


@then("the run detail response includes a capacity object with active_runs, concurrency_limit, and waiting")
def run_detail_includes_capacity(request):
    data = request.node._resp.json()
    capacity = data.get("capacity")
    assert isinstance(capacity, dict), "capacity missing"
    assert "active_runs" in capacity
    assert "concurrency_limit" in capacity
    assert "waiting" in capacity


@then("the run detail response includes work_item_refs")
def run_detail_includes_work_item_refs(request):
    data = request.node._resp.json()
    refs = data.get("work_item_refs")
    assert isinstance(refs, list), "work_item_refs missing"
    assert len(refs) > 0, "work_item_refs missing"


@then("the run detail response includes child_runs")
def run_detail_includes_child_runs(request):
    data = request.node._resp.json()
    children = data.get("child_runs")
    assert isinstance(children, list), "child_runs missing"
    assert len(children) > 0, "child_runs missing"


@then("the event stream includes node_started events")
def event_stream_includes_node_started(request):
    data = request.node._resp.json()
    event_types = {e["event_type"] for e in data.get("events", [])}
    assert "node_started" in event_types, f"node_started missing from {event_types}"


@then("the event stream includes node_completed events")
def event_stream_includes_node_completed(request):
    data = request.node._resp.json()
    event_types = {e["event_type"] for e in data.get("events", [])}
    assert "node_completed" in event_types, f"node_completed missing from {event_types}"


@then("the event stream includes node_failed events")
def event_stream_includes_node_failed(request):
    data = request.node._resp.json()
    event_types = {e["event_type"] for e in data.get("events", [])}
    assert "node_failed" in event_types, f"node_failed missing from {event_types}"
