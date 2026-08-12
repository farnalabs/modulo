"""Unit tests for the error-code registry (agent-failure UX, phase 1).

Covers registry integrity (every legacy alias resolves to a registered dotted
code), the ``map_legacy_code`` / ``class_for`` / ``is_retryable`` lookups, and
the ``harness.unknown`` fallback for unmapped codes.
"""

from modulo.core.pipeline_engine.error_codes import (
    ERROR_CODE_REGISTRY,
    LEGACY_ALIASES,
    class_for,
    is_retryable,
    map_legacy_code,
)


def test_all_legacy_aliases_resolve_to_registered_codes():
    """Every alias points at a dotted code that actually exists in the registry."""
    assert LEGACY_ALIASES
    for legacy, dotted in LEGACY_ALIASES.items():
        assert dotted in ERROR_CODE_REGISTRY, f"{legacy!r} -> {dotted!r} not registered"


def test_core_registry_entries_present_with_expected_attributes():
    """The phase-1 minimum set is present with the right class/severity."""
    agent_failed = ERROR_CODE_REGISTRY["agent.failed"]
    assert agent_failed.error_class == "agent"
    assert agent_failed.retryable is False
    assert agent_failed.alert_severity == "critical"
    assert ERROR_CODE_REGISTRY["agent.no_op"].alert_severity == "warning"
    assert ERROR_CODE_REGISTRY["agent.stall"].alert_severity == "warning"
    assert ERROR_CODE_REGISTRY["contract.schema"].error_class == "contract"
    assert ERROR_CODE_REGISTRY["harness.unknown"].error_class == "harness"


def test_map_legacy_code_legacy_aliases():
    """Legacy codes map to their dotted equivalents per §3.2."""
    assert map_legacy_code("executor_stalled") == "agent.stall"
    assert map_legacy_code("node_timeout") == "node.timeout"
    assert map_legacy_code("TimeoutError") == "node.timeout"
    assert map_legacy_code("executor_superseded") == "run.superseded"
    assert map_legacy_code("output_rejected") == "contract.schema"
    assert map_legacy_code("runaway") == "node.runaway"
    assert map_legacy_code("runaway.tokens_exceeded") == "node.runaway"
    assert map_legacy_code("node_cancelled") == "node.cancelled"
    assert map_legacy_code("eval_blocked") == "eval.blocked"
    assert map_legacy_code("eval_suite_blocked") == "eval.blocked"
    assert map_legacy_code("configuration_error") == "config.error"
    assert map_legacy_code("OperationalError") == "harness.db.connection_lost"
    assert map_legacy_code("TypeError") == "harness.state_serialization"


def test_map_legacy_code_dotted_passthrough():
    """Already-dotted registry codes pass through unchanged."""
    assert map_legacy_code("agent.failed") == "agent.failed"
    assert map_legacy_code("node.timeout") == "node.timeout"
    assert map_legacy_code("harness.unknown") == "harness.unknown"


def test_map_legacy_code_unknown_and_none_fall_back_to_harness_unknown():
    """Unmapped codes and None resolve to the harness.unknown fallback."""
    assert map_legacy_code("some_mystery_code") == "harness.unknown"
    assert map_legacy_code(None) == "harness.unknown"
    assert map_legacy_code("") == "harness.unknown"


def test_class_for_known_and_legacy_codes():
    assert class_for("agent.failed") == "agent"
    assert class_for("executor_stalled") == "agent"
    assert class_for("node_timeout") == "node"
    assert class_for("node.timeout") == "node"
    assert class_for("output_rejected") == "contract"
    assert class_for("connector.invalid_key") == "connector"
    assert class_for("eval_blocked") == "eval"
    assert class_for("configuration_error") == "config"


def test_class_for_unmapped_code_returns_harness():
    """An unmapped legacy code resolves through harness.unknown -> 'harness'."""
    assert class_for("unknown_legacy_code") == "harness"


def test_is_retryable_defaults():
    """Non-retryable by default for work-truth / permanent codes."""
    assert is_retryable("agent.failed") is False
    assert is_retryable("agent.no_op") is False
    assert is_retryable("agent.stall") is False
    assert is_retryable("contract.schema") is False
    assert is_retryable("node.runaway") is False
    assert is_retryable("connector.invalid_key") is False
    assert is_retryable("connector.permission") is False
    assert is_retryable("unknown_code") is False
    assert is_retryable(None) is False


def test_is_retryable_transient_codes_true():
    """Harness/sandbox/connector-transient codes are retryable per §3.2."""
    for code in (
        "node.timeout",
        "node_timeout",  # via alias
        "harness.db.connection_lost",
        "harness.sdk_task_cancelled",
        "harness.worker_failed",
        "harness.dispatch_failed",
        "harness.executor_failed",
        "harness.gate_creation_failed",
        "sandbox.no_output_json",
        "sandbox.spawn",
        "sandbox.network",
        "connector.network",
        "connector.rate_limit",
    ):
        assert is_retryable(code) is True, code
