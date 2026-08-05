"""Unit tests for build_cost_breakdown — flat clamp + marker, string clamp, eval errors (§1.3/§2.4/§4.5)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from modulo.core.cost_controller.breakdown.aggregate import build_cost_breakdown, clamp_reported
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
    assert uuid.uuid4()  # uuid import guard
