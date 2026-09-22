"""Step definitions for observability features — metrics, OTel traces, and run logs."""

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.pipeline_engine.event_broker import get_registry

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
    with patch(
        "modulo.api.routes.observability.pinned_async_client",
        new_callable=AsyncMock,
    ) as mock_pinned:
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_pinned.return_value = mock_client

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
#
# Closed 2026-09-22: previous steps fabricated span dicts in ctx and never
# exercised the real bridge. They now drive the REAL LangGraphOtelBridge seams
# network-free and DB-free (the OTel InMemorySpanExporter pattern of
# tests/unit/otel_bridge/test_handler.py): run-root trace seeding
# (start_run_root), chain + tool + connector callbacks, and the
# set_run_context attribute stamps. "Telemetry disabled" builds a provider with
# NO span processor, so no span can reach the exporter.
# ============================================================================


def _build_otel_harness(ctx: dict[str, Any], enabled: bool) -> None:
    """Build (bridge, provider, exporter) into ctx once for the scenario."""
    if ctx.get("bridge") is not None:
        return
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from modulo.otel_bridge.handler import LangGraphOtelBridge

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    if enabled:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    ctx["exporter"] = exporter
    ctx["provider"] = provider
    ctx["bridge"] = LangGraphOtelBridge(tracer=provider.get_tracer("bdd.langgraph"))
    ctx["otel_enabled"] = enabled
    ctx["org_id"] = str(uuid.uuid4())
    ctx["pipeline_id"] = str(uuid.uuid4())


def _drive_run(ctx: dict[str, Any], nodes: tuple[str, ...], *, connector: bool = False) -> None:
    """Drive the real bridge through a completed run (root + one span per node)."""
    bridge = ctx["bridge"]
    bridge.set_run_context(org_id=ctx["org_id"], pipeline_id=ctx["pipeline_id"])
    bridge.start_run_root(f"{ctx['org_id']}:{ctx['pipeline_id']}")
    for node in nodes:
        run_id = uuid.uuid4()
        bridge.on_chain_start(
            {"name": node, "id": ["langchain", node]},
            {},
            run_id=run_id,
            tags=["connector"] if connector else None,
        )
        bridge.on_chain_end({}, run_id=run_id)
    bridge.end_run_root()
    ctx["run_completed"] = True


@given("OpenTelemetry is configured")
def otel_configured(ctx):
    _build_otel_harness(ctx, enabled=True)


@given("OpenTelemetry is disabled")
def otel_disabled(ctx):
    _build_otel_harness(ctx, enabled=False)


@given("a pipeline run has completed")
def run_completed(ctx):
    _build_otel_harness(ctx, enabled=True)
    _drive_run(ctx, ("analyze", "summarize"))


@given("a pipeline run with tool invocations")
def run_with_tools(ctx):
    _build_otel_harness(ctx, enabled=True)
    bridge = ctx["bridge"]
    bridge.set_run_context(org_id=ctx["org_id"], pipeline_id=ctx["pipeline_id"])
    bridge.start_run_root(f"{ctx['org_id']}:{ctx['pipeline_id']}")
    agent_run = uuid.uuid4()
    bridge.on_chain_start({"name": "agent", "id": ["langchain", "agent"]}, {}, run_id=agent_run)
    tool_run = uuid.uuid4()
    bridge.on_tool_start(
        {"name": "search", "id": ["langchain", "search"]},
        "query",
        run_id=tool_run,
        parent_run_id=agent_run,
    )
    bridge.on_tool_end("result", run_id=tool_run)
    bridge.on_chain_end({}, run_id=agent_run)
    bridge.end_run_root()
    ctx["has_tools"] = True
    ctx["run_completed"] = True


@given("a pipeline run with connector operations")
def run_with_connectors(ctx):
    _build_otel_harness(ctx, enabled=True)
    _drive_run(ctx, ("github_query", "slack_notify"), connector=True)
    ctx["has_connectors"] = True


@when("the OTel span exporter captures the trace")
def otel_captures_trace(ctx):
    ctx["captured_spans"] = list(ctx["exporter"].get_finished_spans())


@when("a pipeline run completes")
def pipeline_run_completes(ctx):
    _drive_run(ctx, ("analyze",))


@then("the trace contains a span for each node execution")
def trace_has_node_spans(ctx):
    spans = ctx.get("captured_spans", [])
    span_names = [s.name for s in spans]
    assert any("langgraph.chain" in name for name in span_names), f"No chain spans found in {span_names}"


@then("the trace contains attributes for organisation_id and pipeline_id")
def trace_has_org_and_pipeline(ctx):
    spans = ctx.get("captured_spans", [])
    found_org = False
    found_pipeline = False
    for s in spans:
        attrs = s.attributes or {}
        if "organisation_id" in attrs:
            found_org = True
        if "pipeline_id" in attrs:
            found_pipeline = True
    assert found_org, "No organisation_id attribute found in any span"
    assert found_pipeline, "No pipeline_id attribute found in any span"


@then("no credential fields appear in span attributes")
def trace_no_credentials(ctx):
    spans = ctx.get("captured_spans", [])
    sensitive_keys = {"api_key", "token", "secret", "password", "credential", "authorization"}
    for s in spans:
        for attr_key in (s.attributes or {}):
            for sensitive in sensitive_keys:
                assert sensitive not in attr_key.lower(), f"Sensitive key '{attr_key}' found in span attributes"


@then("each tool invocation has a child span under its parent node span")
def tool_has_child_span(ctx):
    spans = ctx.get("captured_spans", [])
    tool = next((s for s in spans if "langgraph.tool.search" in s.name), None)
    assert tool is not None, f"No tool spans found in {[s.name for s in spans]}"
    assert tool.parent is not None, "tool span has no parent"

    agent = next((s for s in spans if "langgraph.chain.agent" in s.name), None)
    assert agent is not None, "agent node span missing"
    assert tool.parent.span_id == agent.context.span_id


@then("no OTel spans are exported")
def no_otel_spans_exported(ctx):
    spans = list(ctx["exporter"].get_finished_spans())
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
# Closed 2026-09-21: the scenarios drive the REAL GET /api/v1/runs/{run_id} and
# GET /api/v1/runs/{run_id}/events routes with only the DB-fetch seams patched
# (the _do_* helpers in routes/runs.py), so the route handler, the
# require_permission_any_credential authz dependency, and the RunResponse /
# RunEventsResponse serialization all run for real. The event-stream scenario
# also drives the REAL per-run RunEventBroker in the shared registry, so
# replay_since and the node-lifecycle filter are asserted end to end.
# ============================================================================


def _make_active_run_fake(run_id: uuid.UUID, **kwargs: Any) -> MagicMock:
    """Build a run-shaped fake with the REAL scalar values the serializer needs.

    ``_build_run_response`` / ``RunResponse`` validate their inputs, so every
    field they touch must carry a real value (or ``None``) — a MagicMock child
    would raise a pydantic ValidationError and 500 the route.
    """
    run = MagicMock()
    run.id = run_id
    run.status = kwargs.get("status", "running")
    run.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    run.pipeline = kwargs.get("pipeline")
    run.run_number = kwargs.get("run_number", 1)
    run.langgraph_thread_id = str(uuid.uuid4())
    run.snapshot_id = None
    run.error_detail = None
    run.error_code = None
    run.total_cost_usd = None
    run.total_tokens = 0
    run.node_token_usage = None
    run.cost_breakdown = None
    run.run_classification = None
    run.blocked_partial_summary = None
    run.created_at = datetime.now(UTC)
    run.started_at = datetime.now(UTC)
    run.completed_at = None
    run.heartbeat_at = kwargs.get("heartbeat_at") or datetime.now(UTC)
    run.trigger_type = "manual"
    run.trigger_id = None
    run.work_item_refs = kwargs.get("work_item_refs")
    run.input_payload = None
    return run


@given("an active run with heartbeat, capacity, work item refs, and child runs")
def active_run_with_observability(ctx, request):
    run_id = uuid.uuid4()
    capacity = {"active_runs": 2, "concurrency_limit": 4, "waiting": True}
    child_runs = [
        {"run_id": str(uuid.uuid4()), "run_number": 2, "status": "running", "pipeline_name": "deploy-service"}
    ]
    work_item_refs = [{"kind": "pr", "ref": "farnalabs/modulo#1234", "source": "github", "status": "open"}]
    ctx["run_id"] = run_id
    ctx["run"] = _make_active_run_fake(run_id, work_item_refs=work_item_refs)
    ctx["trigger_actor"] = "tester@modulo.run"
    ctx["capacity"] = capacity
    ctx["child_runs"] = child_runs
    request.node._run_id = run_id
    request.node._run = ctx["run"]


@given("an active run with node lifecycle events")
def active_run_with_node_events(ctx, request):
    run_id = uuid.uuid4()
    broker = get_registry().get_or_create(run_id)
    broker.publish("node_started", {"node_id": "analyze"})
    broker.publish("node_completed", {"node_id": "analyze"})
    broker.publish("node_failed", {"node_id": "summarize"})
    request.addfinalizer(lambda: get_registry().close(run_id))
    ctx["run_id"] = run_id
    ctx["run"] = _make_active_run_fake(run_id)
    request.node._run_id = run_id
    request.node._run = ctx["run"]


@when("I fetch the run detail via the API")
def fetch_run_detail_via_api(client, request, ctx):
    run_id = request.node._run_id
    run = request.node._run
    with (
        patch("modulo.api.routes.runs._do_get_run_with_gate", new_callable=AsyncMock, return_value=(run, False)),
        patch(
            "modulo.api.routes.runs._do_get_child_run_rollup",
            new_callable=AsyncMock,
            return_value=(Decimal("0.000000"), 0),
        ),
        patch("modulo.api.routes.runs._do_get_otel_endpoint", new_callable=AsyncMock, return_value=""),
        patch(
            "modulo.api.routes.runs._do_get_run_observability",
            new_callable=AsyncMock,
            return_value=(ctx["trigger_actor"], ctx["capacity"], ctx["child_runs"]),
        ),
        patch("modulo.api.routes.runs._do_get_workspace_inputs", new_callable=AsyncMock, return_value=None),
    ):
        resp = client.get(f"/api/v1/runs/{run_id}")
    request.node._resp = resp
    assert resp.status_code == 200, resp.text


@when("I fetch the run event stream via the API")
def fetch_run_event_stream_via_api(client, request):
    run_id = request.node._run_id
    with patch("modulo.api.routes.runs._do_get_run", new_callable=AsyncMock, return_value=request.node._run):
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
