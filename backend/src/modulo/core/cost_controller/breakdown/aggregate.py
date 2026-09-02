"""Aggregate the cost breakdown — the single function computing both the
breakdown and the total together (preserving ``total == sum``).

The clamp stack (§4.5) is the LOAD-BEARING anti-abuse set; this module holds
the flat total clamp + marker, the non-finite guard, and the shared clamp
helpers (``_clamp_to_ceiling``, ``_clamp_reported``). The band clamp and the
per-node clamp live at the EXTRACTION boundary (``node_runner``) and in
``_clamp_reported`` (defense-in-depth at enrichment — wired by PR A2).
"""

from __future__ import annotations

import decimal
import logging
from decimal import ROUND_HALF_UP, Decimal
from operator import attrgetter
from typing import Any

from modulo.core.cost_controller.breakdown.constants import (
    COST_COLUMN_CAP,
    MAX_BREAKDOWN_BASIS_SIZE,
    MAX_REPORTABLE_BAND_USD,
    MAX_REPORTABLE_USD_MIN,
    MAX_SELF_REPORTED_USD,
    RAW_REPORTED_DISPLAY_CLAMP,
    TOTAL_CLAMPED_MARKER,
)
from modulo.core.cost_controller.breakdown.formula import CostFormulaError, evaluate_formula
from modulo.core.cost_controller.breakdown.metrics import record_clamped, record_eval_error
from modulo.core.cost_controller.breakdown.params import (
    CALCULATED_ALLOWED_IDENTS,
    CostComponentConfig,
    RunCostTelemetry,
    build_params,
)

_log = logging.getLogger(__name__)

__all__ = [
    "build_cost_breakdown",
    "clamp_reported",
    "clamp_to_ceiling",
]

_QUANT = Decimal("0.000001")


def _clamped_display(value: Decimal) -> float:
    """Serialized display clamp for raw_reported (the money formatter stays sane)."""
    if value.is_finite() and abs(value) <= RAW_REPORTED_DISPLAY_CLAMP:
        return float(value)
    return float(RAW_REPORTED_DISPLAY_CLAMP)


def clamp_to_ceiling(value: Decimal, ceiling: Decimal, kind: str) -> Decimal:
    """Clamp a Decimal value to a ceiling.

    The CALLER decides whether a metric/log is warranted. NaN/Inf never reach
    this helper (the non-finite guard runs first).
    """
    if value > ceiling:
        _log.warning("cost_clamp.to_ceiling", extra={"kind": kind, "value": str(value), "ceiling": str(ceiling)})
        record_clamped(kind)
        return ceiling
    return value


def clamp_reported(value: Decimal | float) -> tuple[Decimal, bool, bool] | None:
    """Re-clamp a folded self-reported model cost at enrichment (defense-in-depth).

    Returns ``(clamped_value, was_clamped_any, out_of_band_high)`` or ``None``
    when the value is treated as ABSENT (bool / non-numeric / NaN/Inf — the
    node's self-report is skipped, it routes to estimate). A stored value below
    the floor is also skipped. ``was_clamped_any`` is TRUE for ANY clamp
    (``clamped != raw``); ``out_of_band_high`` is True iff ``raw > band``.

    This is NOT the flag authority on the live path — the authoritative
    ``model_cost_clamped`` / ``model_cost_out_of_band_high`` folded into the
    union come from the node-output dict written by extraction (PR A2 wires
    the fold). ``clamp_reported``'s own flags are the fallback authority only
    for legacy producers and the pre-migration stored-union-only class.
    """
    if isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not d.is_finite():
        return None
    if d < MAX_REPORTABLE_USD_MIN:
        return None
    raw = d
    clamped = min(raw, MAX_SELF_REPORTED_USD)
    out_of_band_high = raw > MAX_REPORTABLE_BAND_USD
    clamped = min(clamped, MAX_REPORTABLE_BAND_USD)
    return clamped, clamped != raw, out_of_band_high


def _entry_amount(amount: Decimal) -> str:
    """Serialize an amount as a 6dp string, string-clamped to the flat ceiling."""
    return format(min(amount, COST_COLUMN_CAP), "f")


def _basis_within_limit(basis: dict[str, Any]) -> dict[str, Any]:
    """Enforce MAX_BREAKDOWN_BASIS_SIZE per entry.

    On overflow the largest multi-value member (``raw_reported`` per-node map)
    is TRUNCATED deterministically (newest N + ``node_count``). The serialized
    ``raw_reported`` display is clamped to a sane magnitude (1e6) so the UI
    money line cannot render 1e300; the raw value stays in the stored basis for
    audit.
    """
    for key in ("raw_reported", "per_node_raw"):
        raw_val = basis.get(key)
        if isinstance(raw_val, dict):
            display = {}
            for nid, val in raw_val.items():
                try:
                    d = Decimal(str(val))
                    display[nid] = _clamped_display(d)
                except (TypeError, ValueError, ArithmeticError):
                    continue
            basis[key] = display
        elif raw_val is not None:
            try:
                d = Decimal(str(raw_val))
                basis[key] = _clamped_display(d)
            except (TypeError, ValueError, ArithmeticError):
                basis[key] = float(0)
    # Truncate the per-node map when the serialized entry exceeds the bound.
    if len(str(basis).encode("utf-8")) > MAX_BREAKDOWN_BASIS_SIZE:
        for key in ("raw_reported", "per_node_raw"):
            raw_val = basis.get(key)
            if isinstance(raw_val, dict):
                kept = list(raw_val.items())[-8:]
                basis[key] = dict(kept)
                basis["node_count"] = len(kept)
                _log.warning("cost_breakdown.basis_truncated", extra={"key": key})
    return basis


def _eval_calculated(
    component: CostComponentConfig,
    telemetry: RunCostTelemetry,
    settings: Any,
) -> tuple[dict[str, Any], Decimal]:
    entry: dict[str, Any] = {
        "component": component.name,
        "display_name": component.display_name,
        "source": "calculated",
        "amount_usd": "0.000000",
        "formula_applied": component.formula,
        "rate_usd": str(component.rate_usd) if component.rate_usd is not None else None,
    }
    if component.formula is None:
        entry["error"] = "eval_error"
        record_eval_error(component.name)
        return entry, Decimal(0)
    try:
        params = build_params(telemetry, component, settings=settings)
        amount = evaluate_formula(component.formula, params, CALCULATED_ALLOWED_IDENTS)
        entry["basis"] = _basis_within_limit(
            {
                "wall_clock_hours": float(params.get("wall_clock_hours", Decimal(0))),
                "tokens_input": int(telemetry.tokens_input),
                "tokens_output": int(telemetry.tokens_output),
                "nodes_estimated": telemetry.nodes_estimated,
                # Agent-reported token sums (FAR-491) — DISPLAY-ONLY basis
                # surfacing; never an input to the system's built-in money
                # math (operator formulas may reference these identifiers).
                "tokens_input_reported": int(telemetry.tokens_input_reported),
                "tokens_output_reported": int(telemetry.tokens_output_reported),
                "tokens_total_reported": int(telemetry.tokens_total_reported),
                "tokens_cache_read_reported": int(telemetry.tokens_cache_read_reported),
                "tokens_cache_write_reported": int(telemetry.tokens_cache_write_reported),
            }
        )
        amount = amount.quantize(_QUANT, rounding=ROUND_HALF_UP)
        entry["amount_usd"] = _entry_amount(amount)
        return entry, amount
    except (CostFormulaError, decimal.DecimalException, ZeroDivisionError, OverflowError, KeyError) as exc:
        _log.warning(
            "cost_breakdown.eval_error",
            extra={"component": component.name, "exc_class": type(exc).__name__},
        )
        record_eval_error(component.name)
        entry["error"] = "eval_error"
        return entry, Decimal(0)


def _eval_self_reported(
    component: CostComponentConfig,
    telemetry: RunCostTelemetry,
) -> tuple[dict[str, Any], Decimal]:
    rk = component.report_key or "model_cost_usd"
    amount = telemetry.reported.get(rk, Decimal(0)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    raw_vals = {nid: raw for nid, raw in telemetry.raw_reported.items() if nid in _reporting_nodes(telemetry)}
    basis: dict[str, Any] = {
        "reported": float(amount),
        "raw_reported": raw_vals if len(raw_vals) > 1 else next(iter(raw_vals.values()), float(amount)),
        "node_count": len(raw_vals),
    }
    if component.name in telemetry.clamped_nodes or _any_clamped(telemetry):
        basis["clamped"] = True
    missing = rk in telemetry.missing_report_keys
    entry: dict[str, Any] = {
        "component": component.name,
        "display_name": component.display_name,
        "source": "self_reported",
        "amount_usd": _entry_amount(amount),
        "formula_applied": "reported",
        "rate_usd": None,
        "basis": _basis_within_limit(basis),
    }
    if telemetry.eligible_sandbox_node_count > 0:
        entry["missing_self_report"] = missing
    return entry, amount


def _reporting_nodes(telemetry: RunCostTelemetry) -> set[str]:
    """Node ids that contributed to a given report_key (best-effort via raw map)."""
    return set(telemetry.raw_reported)


def _any_clamped(telemetry: RunCostTelemetry) -> bool:
    return bool(telemetry.clamped_nodes)


def build_cost_breakdown(
    telemetry: RunCostTelemetry,
    components: list[CostComponentConfig],
    settings: Any = None,
) -> tuple[list[dict[str, Any]], Decimal]:
    """Build the breakdown list + the summed total (written together).

    Amounts are quantized to 6dp. ``total = sum(amounts)`` in Decimal. NaN
    guard runs BEFORE the flat clamp. When the summed total exceeds the
    Numeric(14,6) column capacity, the total is clamped flat and the breakdown
    is PREFIXED with the synthetic marker entry ``{"total_clamped": true,
    "amount_usd": "0.000000"}`` — the ONE documented exception to
    ``total == sum``. Every component entry's serialized amount string is also
    string-clamped (never ``1E+40``).

    The whole block runs under a ``decimal.localcontext()`` with ONLY
    ``DivisionByZero`` trapped; any other eval failure surfaces as a generic
    ``eval_error`` entry + metric, never a crash.
    """
    live = [c for c in components or [] if c.enabled]
    live.sort(key=attrgetter("sort_order", "name"))

    with decimal.localcontext() as ctx:
        ctx.traps[decimal.DivisionByZero] = True
        breakdown: list[dict[str, Any]] = []
        total = Decimal(0)
        for component in live:
            if component.kind == "self_reported":
                entry, amount = _eval_self_reported(component, telemetry)
            else:
                entry, amount = _eval_calculated(component, telemetry, settings)
            breakdown.append(entry)
            total += amount

        if not total.is_finite():
            _log.warning("cost_breakdown.non_finite_total")
            record_eval_error("total")
            total = Decimal(0)
            breakdown.insert(0, {"component": "total", "error": "eval_error", "amount_usd": "0.000000"})
        elif total > COST_COLUMN_CAP:
            record_clamped("total_flat_clamp")
            total = COST_COLUMN_CAP
            breakdown.insert(0, dict(TOTAL_CLAMPED_MARKER))

        total = total.quantize(_QUANT, rounding=ROUND_HALF_UP)
        return breakdown, total
