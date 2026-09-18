"""Unit tests for run-level cost warnings (missing self-report surfacing).

Pins the fixes for the phantom ``$0.000000`` "Model cost (self-reported)" row:
* ``build_cost_breakdown`` stamps a CLEAR ``missing_self_report_reason`` on a
  zero-amount missing self-report (a non-billing state, not a normal money line);
* ``compute_run_warnings`` / ``compute_run_warnings_count`` derive a run-level
  warning surface from a stored ``cost_breakdown``.
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
