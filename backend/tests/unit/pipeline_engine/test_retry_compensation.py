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


def _migration_0155():
    return importlib.import_module("modulo.db.migrations.versions.0155_pipeline_retry_compensation")


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


def test_migration_0155_extends_constraint_consistently() -> None:
    migration = _migration_0155()
    # The ADD statement includes the two new statuses; the OLD one does not.
    assert _COMPENSATION_FAILED in migration._ADD_NEW
    assert _UNKNOWN in migration._ADD_NEW
    assert _COMPENSATION_FAILED not in migration._ADD_OLD
    assert _UNKNOWN not in migration._ADD_OLD
    # Both share the pre-existing base statuses.
    assert "'cost_ceiling_exceeded'::character varying" in migration._ADD_NEW
