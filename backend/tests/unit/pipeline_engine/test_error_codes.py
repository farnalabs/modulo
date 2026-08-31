"""Unit tests for the error-code registry (agent-failure UX, phase 1).

Covers registry integrity (every legacy alias resolves to a registered dotted
code), the ``map_legacy_code`` / ``class_for`` / ``is_retryable`` lookups, the
``harness.unknown`` fallback for unmapped codes, and the shared
``sanitize_error_text`` / ``present_error`` read-surface helpers.
"""

from modulo.core.pipeline_engine.error_codes import (
    ERROR_CODE_REGISTRY,
    LEGACY_ALIASES,
    class_for,
    expand_code_variants,
    is_retryable,
    known_error_codes,
    map_legacy_code,
    present_error,
    sanitize_error_text,
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


def test_provider_codes_present_with_expected_attributes():
    """Provider codes classify as ``provider``; transient ones are retryable."""
    unavailable = ERROR_CODE_REGISTRY["provider.unavailable"]
    assert unavailable.error_class == "provider"
    assert unavailable.retryable is True
    assert unavailable.alert_severity == "warning"
    auth = ERROR_CODE_REGISTRY["provider.authentication"]
    assert auth.error_class == "provider"
    assert auth.retryable is False
    assert auth.alert_severity == "critical"
    rate_limited = ERROR_CODE_REGISTRY["provider.rate_limited"]
    assert rate_limited.error_class == "provider"
    assert rate_limited.retryable is True
    connection = ERROR_CODE_REGISTRY["provider.connection"]
    assert connection.error_class == "provider"
    assert connection.retryable is True


def test_provider_aliases_resolve_to_provider_class():
    assert class_for("RateLimitError") == "provider"


def test_class_for_unknown_missing_registry_entry(monkeypatch):
    """An unmapped code resolves through harness.unknown to the harness class."""
    assert class_for("nonexistent.code") == "harness"
    # When the resolved code is missing from the registry entirely, the class
    # tag falls back to "unknown" (the registry entry itself is absent).
    monkeypatch.setattr("modulo.core.pipeline_engine.error_codes.map_legacy_code", lambda _c: "totally.missing")
    assert class_for("anything") == "unknown"


def test_is_retryable_unknown_missing_registry_entry(monkeypatch):
    """Unmapped codes default to the harness.unknown retryable value (False)."""
    assert is_retryable("nonexistent.code") is False
    # A code missing from the registry entirely defaults to False too.
    monkeypatch.setattr("modulo.core.pipeline_engine.error_codes.map_legacy_code", lambda _c: "totally.missing")
    assert is_retryable("anything") is False


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
    assert map_legacy_code("RateLimitError") == "provider.rate_limited"
    assert map_legacy_code("ProviderUnavailableError") == "provider.unavailable"
    assert map_legacy_code("AuthenticationError") == "provider.authentication"
    assert map_legacy_code("APIConnectionError") == "provider.connection"


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


def test_non_idempotent_suppression_is_dispatch_site_guard():
    """FAR-295 (merged with #1587): the non-idempotent retry suppression is
    enforced at the executor dispatch site via the graph_idempotent guard, not
    by a dedicated registry code. ``node_cancelled`` stays retryable in the
    registry (policy-level), and the executor guard is what prevents the
    re-dispatch of a run whose graph contains a node with ``idempotent: false``."""
    from modulo.core.pipeline_engine.executor import _graph_is_idempotent

    assert _graph_is_idempotent({"nodes": [{"id": "node-a", "idempotent": False}]}) is False
    assert is_retryable("node_cancelled") is True
    assert "harness.non_idempotent" not in ERROR_CODE_REGISTRY


def test_is_retryable_provider_transient_codes():
    """Transient provider codes are retryable; authentication is permanent."""
    assert is_retryable("provider.unavailable") is True
    assert is_retryable("provider.rate_limited") is True
    assert is_retryable("provider.connection") is True
    assert is_retryable("provider.authentication") is False
    # Raw class names resolve through the aliases to the same defaults.
    assert is_retryable("RateLimitError") is True
    assert is_retryable("ProviderUnavailableError") is True
    assert is_retryable("APIConnectionError") is True
    assert is_retryable("AuthenticationError") is False


def test_expand_code_variants_dotted_input_returns_raw_variants():
    """A dotted input expands to every legacy alias that maps to it."""
    assert expand_code_variants("harness.worker_failed") == {"harness.worker_failed", "task_failure"}
    assert expand_code_variants("agent.stall") == {"agent.stall", "executor_stalled"}
    assert expand_code_variants("node.timeout") == {"node.timeout", "node_timeout", "TimeoutError"}


def test_expand_code_variants_legacy_input_includes_canonical():
    """A legacy input expands to its canonical dotted code and all its aliases."""
    assert expand_code_variants("task_failure") == {"harness.worker_failed", "task_failure"}
    assert "RateLimitError" in expand_code_variants("RateLimitError")
    assert "provider.rate_limited" in expand_code_variants("RateLimitError")


def test_expand_code_variants_unmapped_self_only():
    """An unmapped code has no other spellings."""
    assert expand_code_variants("some_mystery_code") == {"some_mystery_code", "harness.unknown"}


def test_known_error_codes_is_registry_plus_aliases():
    """known_error_codes() is exactly the union of registry keys and aliases."""
    assert known_error_codes() == set(ERROR_CODE_REGISTRY) | set(LEGACY_ALIASES)


def test_known_error_codes_complement_is_the_unknown_fallback_set():
    # The analytics "Unknown error" slice shows every raw code whose
    # map_legacy_code falls back to harness.unknown — precisely the codes NOT
    # in known_error_codes(). Known spellings (dotted, legacy, raw class names)
    # resolve to a known canonical; unmapped spellings resolve to harness.unknown.
    known = known_error_codes()
    for code in ("task_failure", "executor_stalled", "agent.failed", "RateLimitError", "harness.unknown"):
        assert code in known
        assert map_legacy_code(code) != "harness.unknown" or code == "harness.unknown"
    for code in ("ValueError", "ConnectionRefusedError", "SomeMysteryError"):
        assert code not in known
        assert map_legacy_code(code) == "harness.unknown"


def test_known_error_codes_unknown_slice_excludes_harness_unknown_literal():
    # bucket_rows maps a raw literal "harness.unknown" row into the unknown
    # slice (registry passthrough), so the aggregate filter must NOT exclude it
    # — consumers subtract "harness.unknown" from the exclude set. Every other
    # known spelling IS excluded.
    exclude = known_error_codes() - {"harness.unknown"}
    assert "harness.unknown" not in exclude
    assert "task_failure" in exclude
    assert "executor_stalled" in exclude
    assert "agent.failed" in exclude


# ---------------------------------------------------------------------------
# sanitize_error_text / present_error (P4)
# ---------------------------------------------------------------------------


def test_sanitize_is_noop_for_clean_strings():
    """Clean strings pass through verbatim — including the strings pinned by
    the MCP/API tests ("run likely hung", "LLM provider returned 429 ...")."""
    assert sanitize_error_text("run likely hung") == "run likely hung"
    assert sanitize_error_text("LLM provider returned 429 Too Many Requests") == (
        "LLM provider returned 429 Too Many Requests"
    )
    assert not sanitize_error_text("")
    assert not sanitize_error_text(None)


def test_sanitize_redacts_secret_patterns():
    assert "<redacted>" in sanitize_error_text("Bearer tok1234567890")
    assert "Bearer" not in sanitize_error_text("Bearer tok1234567890")
    assert "<redacted>" in sanitize_error_text("key sk-abcdefghijkl1234 here")
    assert "sk-abcdefghijkl1234" not in sanitize_error_text("key sk-abcdefghijkl1234 here")
    assert "<redacted>" in sanitize_error_text("ghp_abcdefghijklmnopqrstuvwxyz12")
    assert "ghp_abcdefghijklmnopqrstuvwxyz12" not in sanitize_error_text("ghp_abcdefghijklmnopqrstuvwxyz12")
    assert "<redacted>" in sanitize_error_text("AKIAABCDEFGHIJKLMNOP")
    assert "<redacted>" in sanitize_error_text("postgres://user:supersecret@localhost/db")


def test_sanitize_is_idempotent():
    sample = "boom: Bearer tok1234567890, key sk-abcdefghijkl1234"
    once = sanitize_error_text(sample)
    twice = sanitize_error_text(once)
    assert once == twice


def test_sanitize_strips_hard_control_characters_only():
    # \n (a legitimate line break) is preserved; NUL and \x07 are stripped.
    assert sanitize_error_text("line1\x00\x07line2\n") == "line1line2\n"


def test_sanitize_caps_input_before_regex():
    long = "x" * 10000
    assert len(sanitize_error_text(long)) == 5000


def test_sanitize_coerces_non_str():
    assert sanitize_error_text(12345) == "12345"
    assert sanitize_error_text(b"bytes") == "b'bytes'"


def test_present_error_canonicalizes_code_via_map_legacy_code():
    # Legacy codes are canonicalized to the dotted taxonomy on every read surface.
    code, detail = present_error("executor_stalled", "detail", 5000)
    assert code == "agent.stall"
    assert detail == "detail"

    # Already-dotted registry codes pass through unchanged.
    code, _ = present_error("agent.failed", "detail", 5000)
    assert code == "agent.failed"

    # Unmapped legacy codes resolve to the harness.unknown fallback.
    code, detail = present_error("rate_limited", "LLM provider returned 429 Too Many Requests", 5000)
    assert code == "harness.unknown"
    assert detail == "LLM provider returned 429 Too Many Requests"


def test_present_error_none_detail_returns_none():
    code, detail = present_error(None, None, 5000)
    assert code is None
    assert detail is None


def test_present_error_none_code_preserved_when_detail_present():
    # A missing code is never turned into harness.unknown — the (None, detail)
    # contract keeps error_code absent on the wire.
    code, detail = present_error(None, "boom", 5000)
    assert code is None
    assert detail == "boom"


def test_present_error_truncates_codepoint_safely_with_ellipsis():
    code, detail = present_error("task_failure", "e" * 300, 200)
    assert code == "harness.worker_failed"
    assert detail.endswith("…")
    assert len(detail) == 201


def test_present_error_sanitizes_before_truncate():
    _code, detail = present_error("task_failure", "sk-abcdefghijkl1234 " + "y" * 300, 200)
    assert "sk-abcdefghijkl1234" not in detail
    assert "<redacted>" in detail
    assert detail.endswith("…")


def test_present_error_coerces_non_str_detail():
    code, detail = present_error("task_failure", 12345, 5000)
    assert code == "harness.worker_failed"
    assert detail == "12345"


# ---------------------------------------------------------------------------
# FAR-296 Phase 2 — script-mode stage-split codes
# ---------------------------------------------------------------------------


def test_script_codes_are_never_retryable():
    """Every post-claim script-mode code is TERMINAL (never retryable).

    Once a script-mode node's process started (fencing lease claimed), a fault
    can never be retried — re-dispatching could double-execute a side effect.
    """
    for code in (
        "script.failed",
        "script.invalid_output",
        "script.side_effect_unknown",
        "script.session_lost",
        "script.budget_killed",
    ):
        assert is_retryable(code) is False, code
        assert ERROR_CODE_REGISTRY[code].error_class == "script"
        assert ERROR_CODE_REGISTRY[code].retryable is False


def test_script_schema_and_no_output_aliases_canonicalize_to_contract():
    """``script.schema_failed`` / ``script.no_output`` canonicalize to ONE string."""
    assert map_legacy_code("script.schema_failed") == "contract.schema"
    assert map_legacy_code("script.no_output") == "contract.no_output"
    assert class_for("script.schema_failed") == "contract"
    assert is_retryable("script.schema_failed") is False
    assert is_retryable("script.no_output") is False


def test_script_exception_class_names_map_to_script_codes():
    """The executor's generic catch publishes exception class names — they map
    to the canonical script.* codes so the never-retryable classification holds."""
    assert map_legacy_code("ScriptFailedError") == "script.failed"
    assert map_legacy_code("ScriptInvalidOutputError") == "script.invalid_output"
    assert map_legacy_code("ScriptSideEffectUnknownError") == "script.side_effect_unknown"
    assert map_legacy_code("ScriptBudgetKilledError") == "script.budget_killed"
    for name in (
        "ScriptFailedError",
        "ScriptInvalidOutputError",
        "ScriptSideEffectUnknownError",
        "ScriptBudgetKilledError",
    ):
        assert is_retryable(name) is False, name


def test_script_codes_are_known_and_expand():
    """The script.* spellings resolve via known_error_codes and expand_code_variants."""
    known = known_error_codes()
    assert "script.failed" in known
    assert "script.side_effect_unknown" in known
    assert "script.schema_failed" in known
    assert "script.no_output" in known
    assert "contract.schema" in expand_code_variants("script.schema_failed")
    assert "contract.no_output" in expand_code_variants("script.no_output")
    assert "script.failed" in expand_code_variants("ScriptFailedError")


def test_script_budget_killed_is_known_and_expands():
    """script.budget_killed is a known code that expands to the class name and
    vice versa (FAR-296 Phase 3b-3 platform-side runtime killer)."""
    known = known_error_codes()
    assert "script.budget_killed" in known
    assert "ScriptBudgetKilledError" in known
    assert "script.budget_killed" in expand_code_variants("ScriptBudgetKilledError")
    assert "ScriptBudgetKilledError" in expand_code_variants("script.budget_killed")
    assert map_legacy_code("script.budget_killed") == "script.budget_killed"
    assert class_for("script.budget_killed") == "script"
    assert class_for("ScriptBudgetKilledError") == "script"


def test_sandbox_rate_limited_is_known_retryable_and_expands():
    """sandbox.rate_limited is a known, RETRYABLE code (FAR-296 Phase 4a E2B
    rate-limit queueing). Both the retryable wrapper ``SandboxRateLimitedError``
    and the un-retried e2b ``RateLimitException`` class name resolve to it — a
    429 must never fall through to the permanent ``harness.unknown``."""
    known = known_error_codes()
    assert "sandbox.rate_limited" in known
    assert "SandboxRateLimitedError" in known
    assert "RateLimitException" in known
    spec = ERROR_CODE_REGISTRY["sandbox.rate_limited"]
    assert spec.error_class == "sandbox"
    assert spec.retryable is True
    assert spec.alert_severity == "warning"
    assert is_retryable("SandboxRateLimitedError") is True
    assert is_retryable("RateLimitException") is True
    assert is_retryable("sandbox.rate_limited") is True
    assert map_legacy_code("SandboxRateLimitedError") == "sandbox.rate_limited"
    assert map_legacy_code("RateLimitException") == "sandbox.rate_limited"
    assert class_for("SandboxRateLimitedError") == "sandbox"
    assert class_for("RateLimitException") == "sandbox"
    assert "sandbox.rate_limited" in expand_code_variants("SandboxRateLimitedError")
    assert "SandboxRateLimitedError" in expand_code_variants("sandbox.rate_limited")
    assert "RateLimitException" in expand_code_variants("sandbox.rate_limited")


# ---------------------------------------------------------------------------
# FAR-296 Phase 4b — queue-timeout and dispatch-time capacity codes
# ---------------------------------------------------------------------------


def test_sandbox_queue_timeout_is_known_retryable_and_expands():
    """sandbox.queue_timeout is a known, RETRYABLE code (FAR-296 Phase 4b).
    Both the retry-exhaustion wrapper ``SandboxQueueTimeoutError`` and the
    legacy ``SandboxRateLimitExhaustedError`` class name resolve to it."""
    known = known_error_codes()
    assert "sandbox.queue_timeout" in known
    assert "SandboxQueueTimeoutError" in known
    assert "SandboxRateLimitExhaustedError" in known
    spec = ERROR_CODE_REGISTRY["sandbox.queue_timeout"]
    assert spec.error_class == "sandbox"
    assert spec.retryable is True
    assert spec.alert_severity == "warning"
    assert is_retryable("SandboxQueueTimeoutError") is True
    assert is_retryable("SandboxRateLimitExhaustedError") is True
    assert is_retryable("sandbox.queue_timeout") is True
    assert map_legacy_code("SandboxQueueTimeoutError") == "sandbox.queue_timeout"
    assert map_legacy_code("SandboxRateLimitExhaustedError") == "sandbox.queue_timeout"
    assert class_for("SandboxQueueTimeoutError") == "sandbox"
    assert class_for("SandboxRateLimitExhaustedError") == "sandbox"
    assert "sandbox.queue_timeout" in expand_code_variants("SandboxQueueTimeoutError")
    assert "SandboxQueueTimeoutError" in expand_code_variants("sandbox.queue_timeout")
    assert "SandboxRateLimitExhaustedError" in expand_code_variants("sandbox.queue_timeout")


def test_sandbox_capacity_exceeded_maps_to_capacity_org():
    """SandboxCapacityExceededError maps to capacity.org (FAR-296 Phase 4b
    dispatch-time capacity gate)."""
    known = known_error_codes()
    assert "SandboxCapacityExceededError" in known
    assert map_legacy_code("SandboxCapacityExceededError") == "capacity.org"
    assert class_for("SandboxCapacityExceededError") == "capacity"
    assert is_retryable("SandboxCapacityExceededError") is True
    assert is_retryable("capacity.org") is True
    assert "capacity.org" in expand_code_variants("SandboxCapacityExceededError")
    assert "SandboxCapacityExceededError" in expand_code_variants("capacity.org")


# ---------------------------------------------------------------------------
# FAR-510 — masked sandbox-agent failure downgrade code
# ---------------------------------------------------------------------------


def test_sandbox_agent_failed_is_known_non_retryable():
    """sandbox.agent_failed is the executor's finalization-downgrade code
    (FAR-510): a sandbox_agent node whose synthetic failure path (generic
    exception, schema validation) RETURNED the stamped failure envelope
    instead of raising is downgraded from "complete" to "failed" at
    finalization. The dotted code the executor writes into ``runs.error_code``
    must resolve through the registry — never fall back to the
    ``harness.unknown`` unknown slice."""
    known = known_error_codes()
    assert "sandbox.agent_failed" in known
    spec = ERROR_CODE_REGISTRY["sandbox.agent_failed"]
    assert spec.error_class == "sandbox"
    assert spec.retryable is False
    assert spec.alert_severity == "critical"
    assert is_retryable("sandbox.agent_failed") is False
    assert map_legacy_code("sandbox.agent_failed") == "sandbox.agent_failed"
    assert class_for("sandbox.agent_failed") == "sandbox"
    assert "sandbox.agent_failed" in expand_code_variants("sandbox.agent_failed")
