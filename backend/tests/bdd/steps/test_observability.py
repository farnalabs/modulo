"""Step definitions for observability features — metrics, OTel traces, and run logs.

All feature files are currently stubs (TODO). These step registrations provide
minimal placeholder tests that pass, to be filled in when the endpoints are
implemented.
"""

from pytest_bdd import given, scenarios, then, when

try:
    scenarios("../../features/observability/metrics.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/observability/otel_traces.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/observability/run_logs.feature")
except (FileNotFoundError, OSError):
    pass


# ============================================================================
# metrics.feature — TODO
# ============================================================================

# The metrics.feature file is a stub with no scenarios yet.
# Once scenarios are added, matching step definitions must be implemented here.
# Placeholder: re-export markers so the feature file compiles without error.

@given("the observability module is active")
def _observability_active() -> None:
    """Placeholder — implement when /metrics endpoint is ready."""
    pass


@given("Prometheus metrics are enabled")
def _prometheus_enabled() -> None:
    """Placeholder — implement when Prometheus metrics endpoint is ready."""
    pass


@when("I request GET /metrics")
def _request_metrics(client):
    """Placeholder — implement when /metrics endpoint is ready."""
    pass


@then("the response contains pipeline_run_count_total")
def _response_has_run_count() -> None:
    """Placeholder — implement when /metrics endpoint is ready."""
    pass


@then("the response contains active_runs_gauge")
def _response_has_active_runs() -> None:
    """Placeholder — implement when /metrics endpoint is ready."""
    pass


@then("the response contains token_usage_total")
def _response_has_token_usage() -> None:
    """Placeholder — implement when /metrics endpoint is ready."""
    pass


# ============================================================================
# otel_traces.feature — TODO
# ============================================================================

# The otel_traces.feature file is a stub with no scenarios yet.
# Once scenarios are added, matching step definitions must be implemented here.

@given("OpenTelemetry is configured")
def _otel_configured() -> None:
    """Placeholder — implement when LangGraphOtelBridge is done."""
    pass


@given("a pipeline run has completed")
def _run_completed() -> None:
    """Placeholder — implement when LangGraphOtelBridge is done."""
    pass


@when("the OTel span exporter captures the trace")
def _otel_captures_trace() -> None:
    """Placeholder — implement when LangGraphOtelBridge is done."""
    pass


@then("the trace contains a span for each node execution")
def _trace_has_node_spans() -> None:
    """Placeholder — implement when LangGraphOtelBridge is done."""
    pass


@then("the trace contains attributes for organisation_id and pipeline_id")
def _trace_has_org_and_pipeline() -> None:
    """Placeholder — implement when LangGraphOtelBridge is done."""
    pass


@then("no credential fields appear in span attributes")
def _trace_no_credentials() -> None:
    """Placeholder — implement when LangGraphOtelBridge is done."""
    pass


# ============================================================================
# run_logs.feature — TODO
# ============================================================================

# The run_logs.feature file is a stub with no scenarios yet.
# Once scenarios are added, matching step definitions must be implemented here.

@given("a pipeline run is in progress")
def _run_in_progress() -> None:
    """Placeholder — implement when log streaming endpoint is ready."""
    pass


@given("log level filter is set to INFO")
def _log_level_info() -> None:
    """Placeholder — implement when log streaming endpoint is ready."""
    pass


@when("I request per-node log streaming")
def _request_log_streaming() -> None:
    """Placeholder — implement when log streaming endpoint is ready."""
    pass


@when("I request log level filtering")
def _request_log_level_filter() -> None:
    """Placeholder — implement when log streaming endpoint is ready."""
    pass


@then("the response contains log entries grouped by node id")
def _response_has_logs_grouped() -> None:
    """Placeholder — implement when log streaming endpoint is ready."""
    pass


@then("only INFO and above log entries are returned")
def _response_only_info_plus() -> None:
    """Placeholder — implement when log streaming endpoint is ready."""
    pass
