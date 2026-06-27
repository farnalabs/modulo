"""Step definitions for Model Backend features.

All features are currently stubs (TODO). Step definitions are minimal
pass-through implementations that satisfy pytest-bdd scenario registration
without executing real backend selection or rate-limiting logic.
"""

import uuid

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Stub features (TODO — register for existence, minimal pass-through)
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/model_backends/configure.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/model_backends/health_check.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for model backend tests."""
    return {}


# ============================================================================
# Backend Selection — stub step definitions
# ============================================================================


@given("a pipeline with a per-node backend override")
def stub_pipeline_with_node_override(ctx):
    """Stub — set up a pipeline that overrides the backend per node."""
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["node_overrides"] = {"node-a": "anthropic/claude-3-opus", "node-b": "openai/gpt-4o"}
    ctx["default_backend"] = "openai/gpt-4o-mini"


@given("an org with a default backend configured")
def stub_org_with_default_backend(ctx):
    """Stub — configure the org-level default model backend."""
    ctx["org_id"] = uuid.uuid4()
    ctx["default_backend"] = "openai/gpt-4o-mini"


@when("the pipeline runs")
def stub_pipeline_runs(ctx):
    """Stub — simulate a pipeline run with backend selection."""
    # TODO: implement backend resolution logic
    ctx["run_id"] = uuid.uuid4()
    ctx["selected_backends"] = {}


@when(parsers.parse("node {node_id} executes"))
def stub_node_executes(node_id: str, ctx):
    """Stub — record which backend was selected for a given node."""
    # TODO: implement actual backend selection lookup per node
    ctx["selected_backends"] = ctx.get("selected_backends") or {}


@then(parsers.parse('the backend for node "{node_id}" is "{expected_backend}"'))
def stub_node_backend_selected(node_id: str, expected_backend: str, ctx):
    """Stub — verify the node-level backend override was applied."""
    # TODO: assert ctx["selected_backends"][node_id] == expected_backend
    pass


@then("the default backend is used for nodes without an override")
def stub_default_backend_applied(ctx):
    """Stub — verify nodes without overrides use the org default."""
    # TODO: iterate over nodes, check unoverridden nodes use default
    pass


@then("the org default backend is used")
def stub_org_default_backend_used(ctx):
    """Stub — verify the org default backend was selected."""
    # TODO: assert ctx["selected_backends"]["default"] == ctx["default_backend"]
    pass


# ============================================================================
# Rate Limiting — stub step definitions
# ============================================================================


@given(parsers.parse("an org with a per-minute token budget of {budget:d}"))
def stub_org_with_token_budget(budget: int, ctx):
    """Stub — set up an org with a defined token budget."""
    ctx["org_id"] = uuid.uuid4()
    ctx["token_budget"] = budget
    ctx["tokens_used"] = 0


@given("the budget is exhausted")
def stub_budget_exhausted(ctx):
    """Stub — simulate that the token budget has been fully consumed."""
    ctx["tokens_used"] = ctx.get("token_budget", 0)
    ctx["budget_exhausted"] = True


@when("a model backend request is made")
def stub_model_backend_request_made(ctx):
    """Stub — simulate a model backend API call."""
    # TODO: implement rate limiter check
    ctx["request_allowed"] = True


@when("the rate limit window resets")
def stub_rate_limit_window_resets(ctx):
    """Stub — simulate the per-minute rate limit window rolling over."""
    ctx["tokens_used"] = 0
    ctx["budget_exhausted"] = False


@then("the request is allowed")
def stub_request_allowed(ctx):
    """Stub — verify the request was permitted."""
    allowed = ctx.get("request_allowed", True)
    assert allowed, "Expected request to be allowed but it was denied"


@then("the request is denied with a rate-limit error")
def stub_request_denied(ctx):
    """Stub — verify the request was rate-limited."""
    allowed = ctx.get("request_allowed", True)
    assert not allowed, "Expected request to be denied but it was allowed"


@then("the request is allowed again")
def stub_request_allowed_again(ctx):
    """Stub — verify the request is permitted after the window reset."""
    allowed = ctx.get("request_allowed", True)
    assert allowed, "Expected request to be allowed after reset but it was denied"
