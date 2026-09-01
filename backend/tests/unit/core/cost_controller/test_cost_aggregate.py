"""Unit tests for build_cost_breakdown — flat clamp + marker, string clamp, eval errors (§1.3/§2.4/§4.5)."""

from __future__ import annotations

from decimal import Decimal

from modulo.core.cost_controller.breakdown.aggregate import (
    _basis_within_limit,
    build_cost_breakdown,
    clamp_reported,
    clamp_to_ceiling,
)
from modulo.core.cost_controller.breakdown.constants import (
    COST_COLUMN_CAP,
    MAX_REPORTABLE_BAND_USD,
    TOTAL_CLAMPED_MARKER,
)
from modulo.core.cost_controller.breakdown.params import CostComponentConfig, RunCostTelemetry


def _tel(**kw: object) -> RunCostTelemetry:
    defaults = {"wall_clock_elapsed_s": Decimal(0)}
    defaults.update(kw)
    return RunCostTelemetry(**defaults)


def _sandbox_comp() -> CostComponentConfig:
    return CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox Infrastructure",
        kind="calculated",
        rate_fallback="e2b_rate",
        formula="rate * wall_clock_hours",
    )


def test_flat_clamp_just_below_boundary() -> None:
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_usd=Decimal("50000.0"),
        formula="rate * wall_clock_hours",
    )
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert total < COST_COLUMN_CAP
    assert all("total_clamped" not in entry for entry in breakdown)
    # total == sum
    assert total == sum(Decimal(e["amount_usd"]) for e in breakdown)


def test_flat_clamp_at_boundary_no_marker() -> None:
    comp = CostComponentConfig(
        name="big",
        display_name="Big",
        kind="calculated",
        rate_usd=Decimal("99999999.999999"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert total == COST_COLUMN_CAP
    assert all("total_clamped" not in entry for entry in breakdown)


def test_flat_clamp_just_above_boundary_marker_first() -> None:
    comp = CostComponentConfig(
        name="big",
        display_name="Big",
        kind="calculated",
        rate_usd=Decimal("99999999.999999"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(7200))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert total == COST_COLUMN_CAP
    assert breakdown[0] == TOTAL_CLAMPED_MARKER
    # amounts unchanged, marker first
    assert breakdown[1]["amount_usd"] == "99999999.999999"


def test_flat_clamp_two_max_components_never_overflows() -> None:
    comps = [
        CostComponentConfig(
            name=f"c{i}",
            display_name=f"C{i}",
            kind="calculated",
            rate_usd=Decimal("99999999.999999"),
            formula="rate * wall_clock_hours",
        )
        for i in range(2)
    ]
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, total = build_cost_breakdown(tele, comps)
    assert total == COST_COLUMN_CAP
    assert breakdown[0] == TOTAL_CLAMPED_MARKER


def test_single_component_alone_over_max_flat_clamped() -> None:
    comp = CostComponentConfig(
        name="huge",
        display_name="Huge",
        kind="calculated",
        rate_usd=Decimal("999999999999.999999"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert total == COST_COLUMN_CAP
    assert breakdown[0] == TOTAL_CLAMPED_MARKER


def test_per_entry_string_clamp_never_scientific() -> None:
    comp = CostComponentConfig(
        name="huge",
        display_name="Huge",
        kind="calculated",
        rate_usd=Decimal("999999999999.999999"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, _total = build_cost_breakdown(tele, [comp])
    assert "1E+40" not in str(breakdown)


def test_division_by_zero_is_eval_error_not_crash() -> None:
    comp = CostComponentConfig(
        name="div0",
        display_name="Div0",
        kind="calculated",
        formula="rate / wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(0))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert total == Decimal(0)
    assert breakdown[0]["error"] == "eval_error"
    assert breakdown[0]["amount_usd"] == "0.000000"


def test_non_finite_total_is_eval_error_zero() -> None:
    comp = CostComponentConfig(
        name="inf",
        display_name="Inf",
        kind="calculated",
        rate_usd=Decimal("1e40"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert total == Decimal(0)
    assert any(e.get("error") == "eval_error" for e in breakdown)


def test_self_reported_entry_shape() -> None:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert len(breakdown) == 1
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["formula_applied"] == "reported"
    assert entry["amount_usd"] == "0.040000"
    assert entry["missing_self_report"] is False
    assert total == Decimal("0.040000")


def test_missing_self_report_when_no_report_for_key() -> None:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=1,
        missing_report_keys={"model_cost_usd"},
    )
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert breakdown[0]["missing_self_report"] is True
    assert total == Decimal(0)


def test_clamp_reported_rejects_invalid() -> None:
    assert clamp_reported(True) is None
    assert clamp_reported("not-a-number") is None
    assert clamp_reported(float("nan")) is None
    assert clamp_reported(float("inf")) is None


def test_clamp_reported_band_high() -> None:
    result = clamp_reported(Decimal("6000.0"))
    assert result is not None
    clamped, was_clamped, out_of_band = result
    assert clamped == MAX_REPORTABLE_BAND_USD
    assert was_clamped is True
    assert out_of_band is True


def test_clamp_reported_band_and_per_node_consistent() -> None:
    result = clamp_reported(6000.0)
    assert result is not None
    clamped, _w, _o = result
    assert clamped == Decimal("50.0")
    assert isinstance(clamped, Decimal)


def test_breakdown_ignores_disabled_components() -> None:
    comp = CostComponentConfig(
        name="disabled",
        display_name="Disabled",
        kind="calculated",
        rate_usd=Decimal("1.0"),
        formula="rate * wall_clock_hours",
        enabled=False,
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert breakdown == []
    assert total == Decimal(0)


def test_component_id_in_breakdown_entries() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox Infrastructure",
        kind="calculated",
        rate_usd=Decimal("0.1332"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, _total = build_cost_breakdown(tele, [comp])
    assert breakdown[0]["component"] == "sandbox_infra"
    assert breakdown[0]["display_name"] == "Sandbox Infrastructure"


# ---------------------------------------------------------------------------
# clamp_to_ceiling
# ---------------------------------------------------------------------------


def test_clamp_to_ceiling_clamps_above_and_records_metric() -> None:
    assert clamp_to_ceiling(Decimal("10.0"), Decimal("5.0"), "kind_x") == Decimal("5.0")
    assert clamp_to_ceiling(Decimal("5.0"), Decimal("5.0"), "kind_x") == Decimal("5.0")
    assert clamp_to_ceiling(Decimal("4.0"), Decimal("5.0"), "kind_x") == Decimal("4.0")


# ---------------------------------------------------------------------------
# clamp_reported — sub-floor + in-band (above-band already covered above)
# ---------------------------------------------------------------------------


def test_clamp_reported_below_floor_is_absent() -> None:
    assert clamp_reported(Decimal("0.0000005")) is None


def test_clamp_reported_in_band_unchanged() -> None:
    result = clamp_reported(Decimal("30.0"))
    assert result is not None
    clamped, was_clamped, out_of_band = result
    assert clamped == Decimal("30.0")
    assert was_clamped is False
    assert out_of_band is False


# ---------------------------------------------------------------------------
# _basis_within_limit — serialization + deterministic truncation
# ---------------------------------------------------------------------------


def test_basis_normalizes_dict_values_and_clamps_display() -> None:
    basis = _basis_within_limit({"raw_reported": {"a": "1.5", "b": 1e300, "c": "garbage"}})
    assert basis["raw_reported"] == {"a": 1.5, "b": 1000000.0}
    assert "c" not in basis["raw_reported"]


def test_basis_non_dict_non_numeric_becomes_zero() -> None:
    assert _basis_within_limit({"raw_reported": "garbage"}) == {"raw_reported": 0.0}


def test_basis_truncates_oversized_per_node_map() -> None:
    raw = {f"node_{i}": 0.123456 for i in range(120)}
    basis = _basis_within_limit({"raw_reported": raw, "node_count": 120})
    assert basis["node_count"] == 8
    assert len(basis["raw_reported"]) == 8


def test_self_reported_oversized_raw_map_truncated_in_breakdown() -> None:
    raw = {f"node_{i}": 0.123456 for i in range(120)}
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.08")},
        raw_reported=raw,
        eligible_sandbox_node_count=120,
    )
    breakdown, _total = build_cost_breakdown(tele, [comp])
    basis = breakdown[0]["basis"]
    assert basis["node_count"] == 8
    assert len(basis["raw_reported"]) == 8


# ---------------------------------------------------------------------------
# self_reported basis shape edges
# ---------------------------------------------------------------------------


def test_self_reported_multi_node_raw_is_a_dict() -> None:
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.08")},
        raw_reported={"n1": 0.03, "n2": 0.05},
        eligible_sandbox_node_count=2,
    )
    breakdown, _total = build_cost_breakdown(tele, [comp])
    basis = breakdown[0]["basis"]
    assert basis["raw_reported"] == {"n1": 0.03, "n2": 0.05}
    assert basis["node_count"] == 2


def test_self_reported_clamped_marker_when_node_clamped() -> None:
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.05")},
        raw_reported={"n1": 0.05},
        eligible_sandbox_node_count=1,
        clamped_nodes=["model_tokens"],
    )
    breakdown, _total = build_cost_breakdown(tele, [comp])
    assert breakdown[0]["basis"]["clamped"] is True


def test_self_reported_omits_missing_flag_without_eligible_nodes() -> None:
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.05")},
        raw_reported={"n1": 0.05},
        eligible_sandbox_node_count=0,
    )
    breakdown, _total = build_cost_breakdown(tele, [comp])
    assert "missing_self_report" not in breakdown[0]


# ---------------------------------------------------------------------------
# _eval_calculated — missing formula
# ---------------------------------------------------------------------------


def test_calculated_without_formula_is_eval_error() -> None:
    comp = CostComponentConfig(
        name="calc",
        display_name="Calc",
        kind="calculated",
        rate_usd=Decimal("1.0"),
        formula=None,
    )
    tele = _tel(wall_clock_elapsed_s=Decimal(3600))
    breakdown, total = build_cost_breakdown(tele, [comp])
    assert breakdown[0]["error"] == "eval_error"
    assert breakdown[0]["amount_usd"] == "0.000000"
    assert total == Decimal(0)


def test_calculated_basis_carries_reported_tokens_display_only() -> None:
    """FAR-491: a calculated component's basis surfaces the agent-reported
    token sums (display-only — the amount comes from the formula, which here
    uses only server-measured values)."""
    comp = CostComponentConfig(
        name="calc",
        display_name="Calc",
        kind="calculated",
        rate_usd=Decimal("1.0"),
        formula="rate * wall_clock_hours",
    )
    tele = _tel(
        wall_clock_elapsed_s=Decimal(3600),
        tokens_input=100,
        tokens_output=50,
        tokens_input_reported=1234,
        tokens_output_reported=567,
        tokens_total_reported=1801,
        tokens_cache_read_reported=100,
        tokens_cache_write_reported=8,
    )
    breakdown, total = build_cost_breakdown(tele, [comp])
    basis = breakdown[0]["basis"]
    assert basis["tokens_input_reported"] == 1234
    assert basis["tokens_output_reported"] == 567
    assert basis["tokens_total_reported"] == 1801
    assert basis["tokens_cache_read_reported"] == 100
    assert basis["tokens_cache_write_reported"] == 8
    assert basis["tokens_input"] == 100
    assert basis["tokens_output"] == 50
    # The amount is rate * wall_clock_hours — reported tokens never touch money.
    assert total == Decimal("1.000000")
