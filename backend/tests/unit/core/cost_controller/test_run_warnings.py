"""Unit tests for run-level cost warnings (missing self-report surfacing).

Pins the fixes for the phantom ``$0.000000`` "Model cost (self-reported)" row:
* ``build_cost_breakdown`` stamps a CLEAR ``missing_self_report_reason`` on a
  zero-amount missing self-report (a non-billing state, not a normal money line);
* ``compute_run_warnings`` / ``compute_run_warnings_count`` derive a run-level
  warning surface from a stored ``cost_breakdown``.

FAR-1305 adds the second, truthful reason: a node that PRESENTED an explicit
``0.0`` which the trust boundary rejected as unproven is not "the agent never
reported" — it is ``zero_report_unproven``.
"""

from __future__ import annotations

from decimal import Decimal

from modulo.core.cost_controller.breakdown.aggregate import build_cost_breakdown
from modulo.core.cost_controller.breakdown.params import (
    CostComponentConfig,
    RunCostTelemetry,
    build_telemetry,
    compute_run_warnings,
    compute_run_warnings_count,
)
from modulo.core.cost_controller.finalize import _enrich_union, _rejected_zero_nodes


def _self_reported_comp() -> CostComponentConfig:
    return CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )


def _missing_breakdown() -> list[dict[str, object]]:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=1,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, _ = build_cost_breakdown(tele, [_self_reported_comp()])
    return breakdown


def test_missing_self_report_zero_amount_is_clear_non_billing_state() -> None:
    """The zero-amount missing entry signals 'no self-reported cost', not a $0 line."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=1,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, total = build_cost_breakdown(tele, [_self_reported_comp()])
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "agent_not_reported"
    assert entry["amount_usd"] == "0.000000"
    assert total == Decimal(0)


def test_reported_self_report_has_no_missing_reason() -> None:
    """A real self-report never carries the missing-reason stamp."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()])
    entry = breakdown[0]
    assert entry["missing_self_report"] is False
    assert "missing_self_report_reason" not in entry


def test_no_eligible_nodes_omits_missing_reason() -> None:
    """No eligible sandbox nodes means no missing-report stamp at all."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=0,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()])
    entry = breakdown[0]
    assert "missing_self_report" not in entry
    assert "missing_self_report_reason" not in entry


def test_compute_run_warnings_emits_missing_self_report() -> None:
    warnings = compute_run_warnings(_missing_breakdown())
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["code"] == "missing_self_report"
    assert warning["severity"] == "warning"
    assert isinstance(warning["message"], str)
    assert warning["message"]


def test_compute_run_warnings_empty_for_normal_report() -> None:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()])
    assert not compute_run_warnings(breakdown)


def test_genuine_zero_report_renders_zero_without_warning() -> None:
    """FAR-653: a PROVEN genuine-zero self-report (classification records the
    report key with amount 0) renders a $0.000000 REAL line with NO
    missing_self_report flag and NO run warning — the phantom-warning class
    for a legitimately-$0.00 run is gone."""
    comps = [_self_reported_comp()]
    entries = {
        "node1": {
            "sandbox_by_map": True,
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "reported_input_tokens": 0,
            "reported_output_tokens": 0,
            "reported_total_tokens": 0,
        }
    }
    tele, _per_node_cost = build_telemetry(entries, comps)
    breakdown, total = build_cost_breakdown(tele, comps)
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is False
    assert "missing_self_report_reason" not in entry
    assert entry["amount_usd"] == "0.000000"
    assert total == Decimal(0)
    assert not compute_run_warnings(breakdown)
    assert compute_run_warnings_count(breakdown) == 0


def test_compute_run_warnings_handles_non_list() -> None:
    assert not compute_run_warnings(None)
    assert not compute_run_warnings("not-a-list")
    assert not compute_run_warnings({"component": "x"})


def test_compute_run_warnings_skips_non_dict_entries() -> None:
    assert not compute_run_warnings(["garbage", 42, None])


def test_compute_run_warnings_count_matches_length() -> None:
    assert compute_run_warnings_count(_missing_breakdown()) == 1
    assert compute_run_warnings_count(None) == 0


# --- FAR-1305: a rejected EXPLICIT zero is not "the agent did not report" ---


def _free_model_node_shapes() -> tuple[dict, dict, dict]:
    """The observed free-model shape (prod run e927db10).

    The producer wrote ``model_cost_usd: 0.0`` into ``output.json`` — the PURE
    RETURN — alongside REAL token usage. Producer-stage extraction refuses the
    unproven zero, so the telemetry envelope carries NO cost key at all; the
    raw ``0.0`` survives only in the pure return.
    """
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "token_usage": {"input": 35170, "output": 3669, "total": 38839},
        }
    }
    telemetry = {
        "node1": {
            "status": "completed",
            "wall_clock_time_ms": 12_050,
            "model_tokens_input": 35170,
            "model_tokens_output": 3669,
            "model_tokens_total": 38839,
        }
    }
    return usage, outputs, telemetry


def test_rejected_explicit_zero_stamps_rejection_marker() -> None:
    """The enrichment records THAT an explicit zero was presented and rejected.

    The trust boundary is unchanged: the unproven zero is still NOT folded in
    as a report — only the diagnostic marker is added so the reason can be
    truthful downstream.
    """
    usage, outputs, telemetry = _free_model_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    # Acceptance criterion 3 — which reports are accepted is UNCHANGED.
    assert "model_cost_usd" not in entry
    assert _rejected_zero_nodes(union) == {"node1"}


def test_rejected_explicit_zero_reason_is_zero_report_unproven() -> None:
    """Acceptance criterion 1: explicit 0.0 + NON-zero tokens -> the truthful reason.

    The breakdown must not claim the agent stayed silent when it demonstrably
    reported ``$0.00`` and the trust boundary refused the report as unproven.
    """
    usage, outputs, telemetry = _free_model_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele, [_self_reported_comp()], rejected_zero_nodes=_rejected_zero_nodes(union)
    )
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "zero_report_unproven"
    assert entry["amount_usd"] == "0.000000"


def test_absent_cost_key_still_reports_agent_not_reported() -> None:
    """Acceptance criterion 2 — a node with NO cost key keeps the old reason."""
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {"node1": {"status": "completed", "summary": "no cost key at all"}}
    telemetry = {"node1": {"status": "completed", "wall_clock_time_ms": 500}}
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    assert "model_cost_rejected" not in union["node1"]
    assert not _rejected_zero_nodes(union)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele, [_self_reported_comp()], rejected_zero_nodes=_rejected_zero_nodes(union)
    )
    entry = breakdown[0]
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "agent_not_reported"


def test_output_carried_unproven_zero_stamps_rejection_marker() -> None:
    """Second stamp site: a row whose OUTPUT (not just its pure return) carries
    the unproven zero — the legacy / no-split-telemetry shape, where
    ``_fold_from_output_obj``'s reject branch fires."""
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "model_tokens_input": 5,
            "model_tokens_output": 5,
            "model_tokens_total": 10,
        }
    }
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True)
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    assert "model_cost_usd" not in entry


def test_rejected_zero_reason_is_not_emitted_without_eligible_node() -> None:
    """No eligible sandbox node -> no ``missing_self_report`` block at all, so
    neither reason is emitted (unchanged FAR-653 gating)."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=0,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()], rejected_zero_nodes={"node1"})
    assert "missing_self_report" not in breakdown[0]
    assert "missing_self_report_reason" not in breakdown[0]
