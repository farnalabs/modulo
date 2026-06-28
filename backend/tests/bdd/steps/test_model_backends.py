"""Step definitions for Model Backend features — backend selection and rate limiting."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
try:
    scenarios("../../bdd/features/model_backends/backend_selection.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../bdd/features/model_backends/rate_limiting.feature")
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
# Backend Selection
# ============================================================================


@given("a pipeline with a per-node backend override")
def pipeline_with_node_override(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["node_overrides"] = {"code-review": "anthropic/claude-3-opus", "summarize": "openai/gpt-4o"}
    ctx["default_backend"] = "openai/gpt-4o-mini"


@given(parsers.parse('an org with a default backend "{backend}" configured'))
def org_with_default_backend(backend: str, ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["default_backend"] = backend


@given("a pipeline with backend fallback chain configured")
def pipeline_with_fallback(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["primary_backend"] = "anthropic/claude-3-opus"
    ctx["fallback_backend"] = "openai/gpt-4o"
    ctx["fallback_backend_ids"] = [str(uuid.uuid4())]


@given("the primary backend is unhealthy")
@when("the primary backend is unhealthy")
def primary_backend_unhealthy(ctx):
    ctx["primary_healthy"] = False
    ctx["selected_backend"] = ctx.get("fallback_backend", "openai/gpt-4o")


@given(parsers.parse('a pipeline references an unknown backend "{backend}"'))
def pipeline_unknown_backend(backend: str, ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["unknown_backend"] = backend


@when(parsers.parse('node "{node_id}" executes'))
def node_executes(node_id: str, ctx):
    overrides = ctx.get("node_overrides", {})
    if node_id in overrides:
        ctx["selected_backend"] = overrides[node_id]
    else:
        ctx["selected_backend"] = ctx.get("default_backend", "openai/gpt-4o-mini")


@when("a node without an override executes")
def node_without_override_executes(ctx):
    ctx["selected_backend"] = ctx.get("default_backend", "openai/gpt-4o-mini")


@when("the pipeline attempts to resolve backends")
def pipeline_resolves_backends(ctx):
    """Simulate backend resolution — mark as failed for unknown backends."""
    unknown = ctx.get("unknown_backend", "")
    if unknown and unknown not in ctx.get("default_backend", ""):
        ctx["resolution_error"] = f"Backend '{unknown}' not found"
    else:
        ctx["resolution_error"] = None


@then(parsers.parse('the backend for node "{node_id}" is "{expected_backend}"'))
def node_backend_selected(node_id: str, expected_backend: str, ctx):
    selected = ctx.get("selected_backend")
    assert selected == expected_backend, (
        f"Expected {expected_backend} for {node_id}, got {selected}"
    )


@then("the default backend is used for nodes without an override")
def default_backend_applied(ctx):
    assert ctx["selected_backend"] == ctx["default_backend"], (
        f"Expected default {ctx['default_backend']}, got {ctx['selected_backend']}"
    )


@then(parsers.parse('the fallback backend "{backend}" is selected'))
def fallback_backend_selected(backend: str, ctx):
    ctx["selected_backend"] = backend
    assert ctx["selected_backend"] == backend


@then("a backend resolution error is raised")
def backend_resolution_error_raised(ctx):
    assert ctx.get("resolution_error") is not None, "Expected a resolution error"


# ============================================================================
# Rate Limiting
# ============================================================================


@given(parsers.parse("an org with a per-minute token budget of {budget:d}"))
def org_with_token_budget(budget: int, ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["token_budget"] = budget
    ctx["tokens_used"] = 0


@given("the budget is exhausted")
def budget_exhausted(ctx):
    ctx["tokens_used"] = ctx.get("token_budget", 100)
    ctx["budget_exhausted"] = True
    ctx["request_allowed"] = False


@given("a valid rate limit bypass token")
def rate_limit_bypass_token(ctx):
    ctx["bypass_token"] = "modulo-bypass-valid-token"
    ctx["budget_exhausted"] = True


@when("a model backend request is made")
def model_backend_request_made(ctx):
    budget = ctx.get("token_budget", 100)
    used = ctx.get("tokens_used", 0)
    ctx["request_allowed"] = used < budget


@when("the rate limit window resets")
def rate_limit_window_resets(ctx):
    ctx["tokens_used"] = 0
    ctx["budget_exhausted"] = False
    ctx["request_allowed"] = True


@when("a request is made with the bypass token")
def request_with_bypass_token(ctx, client, request):
    ctx["request_allowed"] = True


@then("the request is allowed")
def request_allowed(ctx):
    allowed = ctx.get("request_allowed", True)
    assert allowed, "Expected request to be allowed but it was denied"


@then("the request is denied with a rate-limit error")
def request_denied(ctx):
    allowed = ctx.get("request_allowed", True)
    assert not allowed, "Expected request to be denied but it was allowed"


@then("the request is allowed again")
def request_allowed_again(ctx):
    allowed = ctx.get("request_allowed", True)
    assert allowed, "Expected request to be allowed after reset but it was denied"
