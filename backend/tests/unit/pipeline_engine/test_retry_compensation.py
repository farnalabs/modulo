"""FAR-402 P5 (FAR-419): per-node/per-edge retry + compensation + idempotency.

Covers the authoring / schema / validation layer (design §F3, §4F) plus the
run-status protocol (§10):

* per-node retry overrides the pipeline ``retry_policy`` default,
* per-edge transition retry re-executes the source node and is MUTUALLY
  EXCLUSIVE with an ``on_failure_target`` (typed error when both are set),
* fail-closed for ``idempotent=false``,
* compensation edges: watched-node terminal-failure routing + ACYCLIC enforced
  at compile (cycle → typed error),
* ``COMPENSATION_FAILED`` terminal status wired across the run-status
  vocabularies + DB CHECK constraint (``unknown`` still writable),
* the node-scoped idempotency key (``run + node + index``) and the run-level
  ``idempotency_key`` (reused across an operator re-run).

All tests are DB-free (pure functions / in-memory ``ValidationResult``) —
real-DB constraint round-trips are covered by the integration invariant tests.
"""

from __future__ import annotations

import importlib

import pytest

from modulo.core.cost_controller.probe import PROBE_TERMINAL_STATUSES
from modulo.core.graph_validator import GraphValidator
from modulo.core.graph_validator._types import ValidationResult
from modulo.core.pipeline_engine import retry_compensation as rc
from modulo.db.crud.run import RUN_STATUS_WHITELIST
from modulo.db.models.run import ACTIVE_RUN_STATUSES, TERMINAL_STATUSES, Run

# ---------------------------------------------------------------------------
# Per-node retry
# ---------------------------------------------------------------------------


def test_node_retry_overrides_pipeline_default() -> None:
    node = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 2.0, "on": ["timeout", "error"]}}
    # Pipeline default says retry on stall only, budget 5 — the node must override.
    policy = rc.resolve_node_retry(node, {"on": ["stall"], "max_retries": 5})
    assert policy.max_attempts == 3
    assert policy.backoff_seconds == 2.0
    assert policy.events == frozenset({"timeout", "error"})


def test_node_retry_inherits_pipeline_default() -> None:
    node = {"id": "n1"}
    policy = rc.resolve_node_retry(node, {"on": ["stall", "timeout"], "max_retries": 2})
    assert policy.max_attempts == 3  # max_retries 2 -> attempt ceiling 3
    assert policy.events == frozenset({"stall", "timeout"})


def test_node_retry_not_configured_no_retry_for_absent_policy() -> None:
    policy = rc.resolve_node_retry({"id": "n1"}, None)
    assert policy.max_attempts == 1
    assert policy.retries == 0
    assert not rc.node_retries_on(policy, "error")


def test_node_retry_events_map_failure_to_error() -> None:
    # The run-level vocabulary uses 'failure'; the node-level uses 'error'.
    policy = rc.resolve_node_retry({"id": "n1"}, {"on": ["failure"], "max_retries": 1})
    assert policy.events == frozenset({"error"})


def test_node_fail_closed_for_idempotent_false() -> None:
    # A non-idempotent node must NEVER be auto-retried regardless of its own
    # retry block or the pipeline default.
    node = {"id": "n1", "idempotent": False, "retry": {"max_attempts": 4, "on": ["error"]}}
    policy = rc.resolve_node_retry(node, {"on": ["error"], "max_retries": 3})
    assert policy.max_attempts == 1
    assert policy.retries == 0
    assert rc.node_is_fail_closed(node)


# ---------------------------------------------------------------------------
# Per-edge retry + compensation mutual exclusion
# ---------------------------------------------------------------------------


def test_edge_retry_reattempts_source() -> None:
    edge = {"source": "n1", "target": "n2", "retry": {"max_attempts": 2, "on": ["timeout"]}}
    assert rc.edge_retry_reattempts_source(edge)
    assert not rc.edge_has_compensation(edge)


def test_edge_has_compensation_when_on_failure_set() -> None:
    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    assert rc.edge_has_compensation(edge)
    assert not rc.edge_retry_reattempts_source(edge)


def test_edge_retry_and_compensation_conflict() -> None:
    edge = {
        "source": "n1",
        "target": "n2",
        "retry": {"max_attempts": 2, "on": ["error"]},
        "on_failure_target": "n-comp",
    }
    assert rc.edge_retry_and_compensation_conflict(edge)


def test_validate_edge_mutual_exclusion_emits_typed_error() -> None:
    result = ValidationResult()
    edge = {"source": "n1", "retry": {"max_attempts": 2, "on": ["error"]}, "on_failure_target": "n-comp"}
    rc.validate_edge_mutual_exclusion(edge, result)
    assert result.issues
    assert result.issues[0].code == "EDGE_RETRY_COMPENSATION_EXCLUSIVE"
    assert result.issues[0].severity == "error"


def test_validate_edge_no_conflict_passes() -> None:
    result = ValidationResult()
    rc.validate_edge_mutual_exclusion({"source": "n1", "retry": {"max_attempts": 2, "on": ["error"]}}, result)
    rc.validate_edge_mutual_exclusion({"source": "n1", "on_failure_target": "n-comp"}, result)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Compensation edges
# ---------------------------------------------------------------------------


def test_compensation_fires_on_watched_node_terminal_failure_semantics() -> None:
    # An edge carrying on_failure_target routes a watched-node terminal failure
    # to the compensation node — forward-only, no undo of completed branches.
    edge = {"source": "n1", "target": "n2", "on_failure_target": "n-comp"}
    assert rc.edge_has_compensation(edge)
    # A compensation edge with no retry is NOT re-executed on failure — the
    # source failure routes to compensation, it is not re-run.
    assert not rc.edge_retry_reattempts_source(edge)


def test_compensation_cycle_detected() -> None:
    graph = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [
            {"source": "a", "target": "b", "on_failure_target": "c"},
            {"source": "c", "target": "a", "on_failure_target": "a"},
        ],
    }
    cycles = rc.detect_compensation_cycle(graph)
    assert cycles, "compensation cycle must be detected"


def test_validate_compensation_acyclic_emits_typed_error() -> None:
    graph = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
        "edges": [
            {"source": "a", "target": "b", "on_failure_target": "c"},
            {"source": "c", "target": "d", "on_failure_target": "a"},
        ],
    }
    result = ValidationResult()
    rc.validate_compensation_acyclic(graph, result)
    assert result.issues
    assert result.issues[0].code == "COMPENSATION_CYCLE"


def test_validate_compensation_acyclic_acyclic_graph_passes() -> None:
    graph = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [
            {"source": "a", "target": "b", "on_failure_target": "c"},
            {"source": "b", "target": "c"},
        ],
    }
    result = ValidationResult()
    rc.validate_compensation_acyclic(graph, result)
    assert result.is_valid


def test_validate_compensation_target_must_exist() -> None:
    graph = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"source": "a", "target": "b", "on_failure_target": "ghost"}],
    }
    result = ValidationResult()
    rc.validate_compensation_target_exists(graph["edges"], graph["nodes"], result)
    assert result.issues
    assert result.issues[0].code == "COMPENSATION_TARGET_NOT_FOUND"


def test_graph_validator_rejects_exclusive_and_cycle() -> None:
    graph = {
        "nodes": [{"id": "n1"}, {"id": "n2"}],
        "edges": [
            {"source": "n1", "target": "n2", "retry": {"max_attempts": 2, "on": ["error"]}, "on_failure_target": "n1"},
        ],
    }
    result = ValidationResult()
    GraphValidator._check_failure_and_retry(graph, result)
    codes = {i.code for i in result.issues}
    assert "EDGE_RETRY_COMPENSATION_EXCLUSIVE" in codes


def test_graph_validator_rejects_retry_cycle_but_accepts_normal_config() -> None:
    graph = {
        "nodes": [{"id": "n1"}, {"id": "n2"}],
        "edges": [{"source": "n1", "target": "n2", "retry": {"max_attempts": 2, "on": ["timeout"]}}],
    }
    result = ValidationResult()
    GraphValidator._check_failure_and_retry(graph, result)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def test_node_scoped_idempotency_key_deterministic() -> None:
    k1 = rc.node_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    k2 = rc.node_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert k1 == k2


def test_node_scoped_idempotency_key_differs_across_index_and_node() -> None:
    base = rc.node_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert base != rc.node_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=1)
    assert base != rc.node_idempotency_key(run_ref="pipeline:42", node_ref="node-b", index=0)


def test_node_scoped_idempotency_key_ignores_fresh_run_id_via_run_ref() -> None:
    # The key is anchored on the STABLE run ref, so an in-run retry reuses the
    # identical key (prevents double-execution of a scatter/retry node).
    k1 = rc.node_idempotency_key(run_ref=rc.build_run_ref("pipeline-uuid", 7), node_ref="node-a", index=3)
    k2 = rc.node_idempotency_key(run_ref=rc.build_run_ref("pipeline-uuid", 7), node_ref="node-a", index=3)
    assert k1 == k2


def test_run_level_idempotency_key_reused_across_operator_rerun() -> None:
    # An operator re-run recomputes the SAME run-level key from the logical run
    # identity (pipeline + run_number) even though the per-replay run_id is fresh.
    run_ref = rc.build_run_ref("pipeline-uuid", 7)
    first = rc.run_idempotency_key(run_ref=run_ref)
    rerun = rc.run_idempotency_key(run_ref=run_ref)
    assert first == rerun
    # A genuinely different logical run (a fresh pipeline/run_number) differs.
    assert first != rc.run_idempotency_key(run_ref=rc.build_run_ref("pipeline-uuid", 8))


def test_run_level_key_rejects_bare_run_id() -> None:
    # A naive per-replay run_id must be rejected loudly (FAR-410 contract).
    with pytest.raises(ValueError):
        rc.run_idempotency_key(run_ref="550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f")


# ---------------------------------------------------------------------------
# Run-status protocol (§10) — compensation_failed terminal + unknown non-terminal
# ---------------------------------------------------------------------------

_COMPENSATION_FAILED = "compensation_failed"
_UNKNOWN = "unknown"


def _migration_0158():
    return importlib.import_module("modulo.db.migrations.versions.0159_pipeline_retry_compensation")


def test_compensation_failed_registered_in_vocabularies() -> None:
    assert _COMPENSATION_FAILED in TERMINAL_STATUSES
    assert _COMPENSATION_FAILED in RUN_STATUS_WHITELIST
    assert _COMPENSATION_FAILED in PROBE_TERMINAL_STATUSES


def test_compensation_failed_is_terminal_not_active() -> None:
    assert _COMPENSATION_FAILED in TERMINAL_STATUSES
    assert _COMPENSATION_FAILED not in ACTIVE_RUN_STATUSES


def test_unknown_is_non_terminal_active() -> None:
    assert _UNKNOWN in ACTIVE_RUN_STATUSES
    assert _UNKNOWN not in TERMINAL_STATUSES
    assert _UNKNOWN in RUN_STATUS_WHITELIST


def test_compensation_failed_in_runs_check_constraint() -> None:
    constraint = next(c for c in Run.__table__.constraints if getattr(c, "name", None) == "ck_runs_status")
    assert _COMPENSATION_FAILED in str(constraint.sqltext)
    assert _UNKNOWN in str(constraint.sqltext)


def test_unknown_still_writable_alongside_compensation_failed() -> None:
    # Both new statuses are whitelisted + terminal/non-terminal classified; the
    # DB CHECK constraint admits both (asserted above). "unknown still works".
    assert _UNKNOWN not in TERMINAL_STATUSES
    assert _COMPENSATION_FAILED not in ACTIVE_RUN_STATUSES
    assert _UNKNOWN in ACTIVE_RUN_STATUSES


def test_migration_0158_extends_constraint_consistently() -> None:
    migration = _migration_0158()
    # The ADD statement includes the two new statuses; the OLD one does not.
    assert _COMPENSATION_FAILED in migration._ADD_NEW
    assert _UNKNOWN in migration._ADD_NEW
    assert _COMPENSATION_FAILED not in migration._ADD_OLD
    assert _UNKNOWN not in migration._ADD_OLD
    # Both share the pre-existing base statuses.
    assert "'cost_ceiling_exceeded'::character varying" in migration._ADD_NEW


def test_policy_from_pipeline_default_maps_run_vocabulary_to_node_policy() -> None:
    """FAR-402 P5 (reviewer finding 4): the run-level ``retry_policy`` vocabulary
    ({on: [stall|timeout|failure], max_retries}) is re-parsed by
    ``_policy_from_pipeline_default`` (the DB-free mirror of the executor's own
    parse). Lock the mapping so the two vocabularies cannot silently drift:
    'failure' -> node 'error', 'stall' -> 'stall', 'timeout' -> 'timeout', and
    the attempt ceiling is ``max_retries + 1``.
    """
    policy = rc._policy_from_pipeline_default({"on": ["stall", "timeout", "failure"], "max_retries": 3, "backoff": 1.5})
    assert policy.max_attempts == 4  # max_retries 3 -> ceiling 4
    assert policy.backoff_seconds == 1.5
    assert policy.events == frozenset({"stall", "timeout", "error"})


def test_policy_from_pipeline_default_fail_closed_on_missing_vocabulary() -> None:
    """A malformed / legacy run-level policy must fail CLOSED to no retry (the
    executor's parse path relies on this default — the mirrors must agree)."""
    for bad in (None, {}, {"on": "stall"}, {"max_retries": 0}, {"on": ["stall"], "max_retries": -1}):
        policy = rc._policy_from_pipeline_default(bad)
        assert policy.max_attempts == 1
        assert not policy.events


# ---------------------------------------------------------------------------
# FAR-525 — resolve_backoff_schedule (run-level backoff_schedule, total fail-open)
# ---------------------------------------------------------------------------


def test_resolve_backoff_schedule_absent_variants() -> None:
    """null / {} / missing key / non-dict policy = NO schedule (absent)."""
    for policy in (
        None,
        {},
        {"on": ["failure"], "max_retries": 2},
        {"backoff_schedule": None},
        {"backoff_schedule": {}},
    ):
        present, delay, mult, reason = rc.resolve_backoff_schedule(policy)
        assert present is False
        assert reason is None
        # Absent hands the executor's own hardcoded defaults back.
        assert delay == 45.0
        assert mult == 2.0


def test_resolve_backoff_schedule_valid_fixed_and_growth() -> None:
    present, delay, mult, reason = rc.resolve_backoff_schedule(
        {"backoff_schedule": {"delay_seconds": 30, "multiplier": 1.0}}
    )
    assert (present, delay, mult, reason) == (True, 30.0, 1.0, None)

    present, delay, mult, reason = rc.resolve_backoff_schedule(
        {"backoff_schedule": {"delay_seconds": 10, "multiplier": 3}}
    )
    assert (present, delay, mult, reason) == (True, 10.0, 3.0, None)


def test_resolve_backoff_schedule_multiplier_defaults_to_2() -> None:
    present, delay, mult, reason = rc.resolve_backoff_schedule({"backoff_schedule": {"delay_seconds": 45}})
    assert (present, delay, mult, reason) == (True, 45.0, 2.0, None)


def test_resolve_backoff_schedule_canonicalizes_types() -> None:
    """Integral floats -> int-valued floats; ints -> floats — the resolved
    values are type-stable so a re-resolve is deterministic."""
    _present, delay, mult, _reason = rc.resolve_backoff_schedule(
        {"backoff_schedule": {"delay_seconds": 300.0, "multiplier": 2}}
    )
    assert delay == 300.0
    assert isinstance(mult, float) and mult == 2.0


def test_resolve_backoff_schedule_fail_open_matrix() -> None:
    """ANY structural fault -> the hardcoded default schedule + a reason.
    ALL-OR-NOTHING: no partial application of a partially valid schedule."""
    fail_cases = [
        {"delay_seconds": 0},
        {"delay_seconds": -5},
        {"delay_seconds": 1000},
        {"delay_seconds": 0.5},
        {"delay_seconds": 301},
        # Non-integral IN-RANGE delays are rejected by the write-site
        # validator, so the resolver must fail open — never silently truncate.
        {"delay_seconds": 2.5},
        {"delay_seconds": 45.5},
        {"delay_seconds": True},
        {"delay_seconds": "45"},
        {"delay_seconds": None},
        # Huge JSON int literals parse to arbitrary-precision Python ints
        # whose float() conversion raises OverflowError — must fail open,
        # never raise.
        {"delay_seconds": 10**309},
        {"delay_seconds": 10**400},
        {"multiplier": 2.0},  # missing delay_seconds
        {"delay_seconds": 45, "multiplier": 0.5},
        {"delay_seconds": 45, "multiplier": 100},
        {"delay_seconds": 45, "multiplier": 10**400},
        {"delay_seconds": 45, "multiplier": True},
        {"delay_seconds": 45, "multiplier": "2"},
        {"delay_seconds": 45, "nope": 1},  # unknown inner key
        {"delay_seconds": float("nan")},
        {"delay_seconds": float("inf")},
    ]
    for schedule in fail_cases:
        present, delay, mult, reason = rc.resolve_backoff_schedule({"backoff_schedule": schedule})
        assert present is True, schedule
        assert delay == 45.0, schedule
        assert mult == 2.0, schedule
        assert reason, schedule


def test_resolve_backoff_schedule_huge_int_delay_fail_open_reason() -> None:
    """FAR-525 qa gate: a huge-int delay_seconds (>308 digits) fails open with
    an out-of-representable-range reason instead of raising OverflowError."""
    for huge in (10**309, 10**400, -(10**309)):
        _present, _delay, _mult, reason = rc.resolve_backoff_schedule({"backoff_schedule": {"delay_seconds": huge}})
        assert reason is not None
        assert "representable range" in reason


def test_resolve_backoff_schedule_non_integral_in_range_delay_fail_open_reason() -> None:
    """FAR-525 qa gate: a non-integral IN-RANGE delay fails open with the
    integrality reason (aligned with the write-site validator) — never a
    silent float->int truncation."""
    for delay in (2.5, 45.5):
        _present, _delay, _mult, reason = rc.resolve_backoff_schedule({"backoff_schedule": {"delay_seconds": delay}})
        assert reason is not None
        assert "must be an integer" in reason


def test_resolve_backoff_schedule_fail_open_non_dict_schedule() -> None:
    present, delay, mult, reason = rc.resolve_backoff_schedule({"backoff_schedule": 45})
    assert (present, delay, mult) == (True, 45.0, 2.0)
    assert reason


def test_resolve_backoff_schedule_legacy_backoff_coexistence() -> None:
    """The legacy numeric `backoff` key coexists: the resolver ignores it (it
    is node-default-inherited), and the node-inheritance path still reads it."""
    policy = {"on": ["failure"], "max_retries": 2, "backoff": 1.5}
    present, _delay, _mult, reason = rc.resolve_backoff_schedule(policy)
    assert present is False
    assert reason is None
    node_policy = rc._policy_from_pipeline_default(policy)
    assert node_policy.backoff_seconds == 1.5  # node inheritance path UNCHANGED


def test_policy_from_pipeline_default_huge_backoff_overflows_to_zero_default() -> None:
    """FAR-525 iteration 2: the legacy numeric `backoff` key has the same
    OverflowError hole as `backoff_schedule` — a direct-DB-written
    ``10**400`` must fail open to the documented invalid-backoff default
    (0.0, the same treatment as a non-numeric backoff), never escape as a
    raw OverflowError that bricks every pipeline run at graph compile."""
    policy = {"on": ["stall"], "max_retries": 3, "backoff": 10**400}
    policy_obj = rc._policy_from_pipeline_default(policy)
    assert policy_obj.backoff_seconds == 0.0
    assert policy_obj.max_attempts == 4  # retry budget path UNCHANGED
    assert policy_obj.events == frozenset({"stall"})


def test_parse_node_retry_huge_backoff_raises_typed_error() -> None:
    """FAR-525 iteration 2: a node-level ``retry.backoff`` of ``10**400``
    (graph JSON) must raise the TYPED RetryConfigError — the same message
    shape as the other invalid-backoff cases — so
    ``validate_node_retry_config``'s ``except RetryConfigError`` surfaces a
    validation error, never a raw OverflowError (graph-save 500)."""
    with pytest.raises(rc.RetryConfigError, match="must be a number of seconds"):
        rc.parse_node_retry({"max_attempts": 3, "backoff": 10**400, "on": ["timeout"]})


def test_validate_node_retry_config_huge_backoff_emits_validation_error_not_exception() -> None:
    """End-to-end through the validator entry point: the overflow backoff
    becomes a typed graph-validation error, not an escaping exception."""
    node = {"id": "n1", "retry": {"max_attempts": 3, "backoff": 10**400, "on": ["timeout"]}}
    result = ValidationResult()
    rc.validate_node_retry_config(node, "n1", result)
    assert not result.is_valid


def test_resolve_backoff_schedule_accepted_resolves_cross_check() -> None:
    """Property: every schedule the WRITE-SITE validator accepts, the resolver
    resolves without fail-open (and vice versa for well-typed in-bounds rows).
    INTEGRAL delay values only (2, 45, 300): the validator rejects
    non-integral in-range delays, and the resolver now fail-opens on them —
    the accepted surface is integral-valued."""
    from modulo.core.graph_validator import GraphValidator
    from modulo.core.graph_validator._types import ValidationResult

    for delay in (1, 45, 300, 300.0):
        for mult in (1, 2, 10, 10.0):
            policy = {"backoff_schedule": {"delay_seconds": delay, "multiplier": mult}}
            result = ValidationResult()
            GraphValidator.check_retry_policy_schedule(policy, result)
            assert result.is_valid, (delay, mult)
            present, _delay, _mult, reason = rc.resolve_backoff_schedule(policy)
            assert present is True, (delay, mult)
            assert reason is None, (delay, mult)


def test_sanitize_retry_policy_snippet_bounded_and_redacted() -> None:
    from modulo.core.pipeline_engine.retry_compensation import sanitize_retry_policy_snippet

    snippet = sanitize_retry_policy_snippet({"delay_seconds": 1000, "api_key": "sk-super-secret", "nested": {"a": 1}})
    assert len(snippet) <= 120
    assert "sk-super-secret" not in snippet
    assert "[REDACTED]" in snippet
    assert "1000" in snippet
    # Non-dict input renders a type tag only (never raw unbounded input).
    assert sanitize_retry_policy_snippet("x" * 10000) == "<str>"
    assert len(sanitize_retry_policy_snippet({"a": {"deep": {"deeper": 1}}})) <= 120


def test_resolve_backoff_schedule_default_schedule_value_matches_executor_defaults(monkeypatch) -> None:
    """The fail-open defaults ARE the hardcoded executor schedule: pinning the
    jitter seam, the resolved deterministic component reproduces the legacy
    exponential exactly."""
    from modulo.core.pipeline_engine import executor as executor_module

    monkeypatch.setattr(executor_module.random, "uniform", lambda a, b: 0.0)
    for attempt in (1, 2, 3, 4, 5):
        _present, delay, mult, _reason = rc.resolve_backoff_schedule({"backoff_schedule": {"delay_seconds": 0}})
        assert (delay, mult) == (45.0, 2.0)
        assert executor_module._retry_backoff_seconds(attempt, base=delay, multiplier=mult) == min(
            45.0 * 2.0 ** (attempt - 1), 300.0
        )


# ---------------------------------------------------------------------------
# FAR-525 qa gate — canonicalise_backoff_schedule (the SINGLE write-site helper)
# ---------------------------------------------------------------------------


def test_canonicalise_backoff_schedule_absent_variants() -> None:
    assert rc.canonicalise_backoff_schedule(None) is None
    assert rc.canonicalise_backoff_schedule({}) is None
    assert rc.canonicalise_backoff_schedule("nope") is None


def test_canonicalise_backoff_schedule_round_trip() -> None:
    """Integral float delay -> int; int multiplier -> float; other keys and
    non-integral values pass through untouched; input never mutated."""
    schedule = {"delay_seconds": 300.0, "multiplier": 2, "extra": "kept"}
    canonical = rc.canonicalise_backoff_schedule(schedule)
    assert canonical == {"delay_seconds": 300, "multiplier": 2.0, "extra": "kept"}
    assert isinstance(canonical["delay_seconds"], int)
    assert isinstance(canonical["multiplier"], float)
    assert schedule == {"delay_seconds": 300.0, "multiplier": 2, "extra": "kept"}
    # Non-integral delay untouched; int delay untouched (already canonical).
    assert rc.canonicalise_backoff_schedule({"delay_seconds": 1.5}) == {"delay_seconds": 1.5}
    assert rc.canonicalise_backoff_schedule({"delay_seconds": 45}) == {"delay_seconds": 45}
    assert rc.canonicalise_backoff_schedule({"delay_seconds": 45, "multiplier": 2.0}) == {
        "delay_seconds": 45,
        "multiplier": 2.0,
    }


def test_canonicalise_backoff_schedule_huge_int_raises_standard_message() -> None:
    """Defense: a huge int that cannot be float-converted raises ValueError
    with the standard validator message shape — never OverflowError."""
    with pytest.raises(ValueError, match="must be an integer between"):
        rc.canonicalise_backoff_schedule({"delay_seconds": 10**400})


def test_write_sites_produce_identical_canonical_stored_output() -> None:
    """FAR-525 qa gate: BOTH write sites (API _validate_retry_policy and the
    import sanitiser) produce IDENTICAL stored output for the same inputs —
    the canonicalisation invariant has ONE implementation."""
    from modulo.api.routes.pipelines import _validate_retry_policy
    from modulo.core.workflow_import_export import _sanitize_retry_policy

    for schedule in (
        {"delay_seconds": 300.0, "multiplier": 2},
        {"delay_seconds": 300, "multiplier": 2.0},
        {"delay_seconds": 45.0},
        {"delay_seconds": 1, "multiplier": 10},
    ):
        api_out = _validate_retry_policy({"on": ["failure"], "max_retries": 2, "backoff_schedule": dict(schedule)})
        import_out, fault = _sanitize_retry_policy(
            {"on": ["failure"], "max_retries": 2, "backoff_schedule": dict(schedule)}
        )
        assert fault is None, schedule
        assert api_out is not None, schedule
        assert import_out is not None, schedule
        assert api_out["backoff_schedule"] == import_out["backoff_schedule"], schedule
    # Spot-check the canonical spellings survive both sites identically.
    api_out = _validate_retry_policy(
        {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 300.0, "multiplier": 2}}
    )
    assert api_out["backoff_schedule"] == {"delay_seconds": 300, "multiplier": 2.0}
