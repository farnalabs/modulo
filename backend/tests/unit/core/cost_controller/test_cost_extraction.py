"""Unit tests for the extraction authority (_extract_reported_cost) + node-output fields (§4.1/§4.5)."""

from __future__ import annotations

import math

import pytest

from modulo.core.cost_controller.breakdown.constants import MAX_REPORTABLE_BAND_USD, MAX_REPORTABLE_USD_MIN
from modulo.core.pipeline_engine.node_runner import _build_model_cost_fields, _extract_reported_cost


def _extract(output: dict, **kw: float) -> tuple[float, float, bool, bool] | None:
    return _extract_reported_cost(output, **kw)


def test_absent_key_returns_none() -> None:
    assert _extract_reported_cost({"summary": "x"}) is None
    assert _extract_reported_cost(None) is None
    assert _extract_reported_cost("not-a-dict") is None


def test_non_numeric_and_bool_rejected() -> None:
    assert _extract_reported_cost({"model_cost_usd": "abc"}) is None
    assert _extract_reported_cost({"model_cost_usd": True}) is None
    assert _extract_reported_cost({"model_cost_usd": False}) is None


def test_negative_zero_nan_inf_rejected() -> None:
    assert _extract_reported_cost({"model_cost_usd": -1.0}) is None
    assert _extract_reported_cost({"model_cost_usd": 0.0}) is None
    assert _extract_reported_cost({"model_cost_usd": float("nan")}) is None
    assert _extract_reported_cost({"model_cost_usd": float("inf")}) is None
    assert _extract_reported_cost({"model_cost_usd": float("-inf")}) is None


def test_sub_floor_is_not_a_report() -> None:
    tiny = float(MAX_REPORTABLE_USD_MIN) / 10
    assert _extract_reported_cost({"model_cost_usd": tiny}) is None


def test_band_clamp_at_boundary() -> None:
    # raw 6000 -> clamped at the band ceiling (50), out_of_band True.
    result = _extract_reported_cost({"model_cost_raw_usd": 6000.0, "model_cost_usd": 50.0})
    assert result is not None
    raw, clamped, was_clamped, out_of_band = result
    assert raw == 6000.0
    assert clamped == float(MAX_REPORTABLE_BAND_USD)
    assert was_clamped is True
    assert out_of_band is True


def test_at_band_ceiling_not_out_of_band() -> None:
    result = _extract_reported_cost({"model_cost_usd": float(MAX_REPORTABLE_BAND_USD)})
    assert result is not None
    _raw, clamped, was_clamped, out_of_band = result
    assert clamped == float(MAX_REPORTABLE_BAND_USD)
    assert was_clamped is False
    assert out_of_band is False


def test_just_below_band_ceiling_ok() -> None:
    result = _extract_reported_cost({"model_cost_usd": 49.99})
    assert result is not None
    _raw, clamped, was_clamped, out_of_band = result
    assert clamped == pytest.approx(49.99)
    assert was_clamped is False
    assert out_of_band is False


def test_raw_source_precedence() -> None:
    # raw 6000 with clamped-on-wire 50 -> flags derive from the TRUE raw.
    result = _extract_reported_cost({"model_cost_raw_usd": 6000.0, "model_cost_usd": 50.0})
    assert result is not None
    raw, _clamped, was_clamped, out_of_band = result
    assert raw == 6000.0
    assert was_clamped is True
    assert out_of_band is True


def test_normal_report() -> None:
    result = _extract_reported_cost({"model_cost_usd": 0.04})
    assert result is not None
    raw, clamped, was_clamped, out_of_band = result
    assert raw == pytest.approx(0.04)
    assert clamped == pytest.approx(0.04)
    assert was_clamped is False
    assert out_of_band is False


def test_schema_drift_flag_returns_none() -> None:
    assert _extract_reported_cost({"schema_drift": True, "model_cost_usd": 5.0}) is None


def test_build_fields_absent_when_no_report() -> None:
    assert _build_model_cost_fields({"summary": "x"}) == {}


def test_build_fields_band_clamped_and_flags_unconditional() -> None:
    fields = _build_model_cost_fields({"model_cost_raw_usd": 6000.0, "model_cost_usd": 50.0})
    assert fields["model_cost_usd"] == float(MAX_REPORTABLE_BAND_USD)
    assert fields["model_cost_raw_usd"] == 6000.0
    assert fields["model_cost_display_usd"] == float(MAX_REPORTABLE_BAND_USD)
    assert fields["model_cost_clamped"] is True
    assert fields["model_cost_out_of_band_high"] is True


def test_build_fields_false_flags_written_explicitly() -> None:
    fields = _build_model_cost_fields({"model_cost_usd": 0.04})
    assert fields["model_cost_clamped"] is False
    assert fields["model_cost_out_of_band_high"] is False


def test_display_clamp_bounds_raw() -> None:
    # A 1e300 raw value: the display field is clamped, the raw rides for audit.
    fields = _build_model_cost_fields({"model_cost_raw_usd": 1e300, "model_cost_usd": 1e300})
    assert fields["model_cost_display_usd"] <= 1e6
    assert fields["model_cost_raw_usd"] == pytest.approx(1e300)


def test_extraction_is_order_independent() -> None:
    """Band clamp and per-node clamp agree regardless of order (band < per-node cap)."""
    high = {"model_cost_raw_usd": 6000.0, "model_cost_usd": 50.0}
    result = _extract_reported_cost(high, per_node_cap=10000.0, max_reportable_band_usd=50.0)
    assert result is not None
    _raw, clamped, was_clamped, out_of_band = result
    assert clamped == 50.0
    assert was_clamped is True
    assert out_of_band is True
    assert math.isfinite(clamped)
