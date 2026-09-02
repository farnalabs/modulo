"""Param registry + telemetry builder for the cost breakdown engine.

The registry is the fixed identifier surface operators may reference in a
formula (§2.2). Every identifier's v1 consumer is stated. The engine is
fail-closed: an identifier not in the registry (for the component's kind) is
rejected at save time AND eval time.

The telemetry builder (``build_telemetry``) is the SINGLE classification
authority: a node is self-reporting iff (1) positive ``model_cost_usd`` >=
floor, (2) ``sandbox_by_map`` (from the run-frozen node-type map via the
enriched union), and (3) an enabled consuming ``self_reported`` component's
``report_key`` matches. Token sums come in TWO flavours: ``tokens_input`` /
``tokens_output`` / ``tokens_estimated`` are SERVER-MEASURED ONLY, while the
``tokens_*_reported`` family sums the agent-supplied ``token_usage`` folded
into the union's ``reported_*`` keys (FAR-491, sandbox nodes included). The
reported family is DISPLAY-ONLY: it surfaces in the breakdown ``basis`` and
operator formulas, but never feeds a cost calculation the system itself
computes — the ``llm_tokens`` money math stays server-measured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from modulo.core.cost_controller.breakdown.constants import (
    MAX_REPORTABLE_USD_MIN,
)

_log = logging.getLogger(__name__)

__all__ = [
    "CALCULATED_ALLOWED_IDENTS",
    "REGISTERED_RATE_FALLBACKS",
    "CostComponentConfig",
    "RunCostTelemetry",
    "build_params",
    "build_telemetry",
]

# The ONLY registered rate-fallback name. CRUD rejects any other name with a
# 422 listing this set. A typo can no longer silently zero ``sandbox_infra``
# via an unresolved fallback.
REGISTERED_RATE_FALLBACKS = frozenset({"e2b_rate"})

# Built-in, read-only default token rates (today's constants in executor.py).
INPUT_TOKEN_RATE = Decimal("0.00001")
OUTPUT_TOKEN_RATE = Decimal("0.00003")

# The param registry — identifier -> (type, meaning, v1 consumer). The
# formula-visible surface. The internal telemetry field for wall-clock is
# ``wall_clock_elapsed_s`` (NEVER a registry identifier); ``wall_clock_hours``
# is the SOLE wall-clock identifier.
_PARAM_REGISTRY: dict[str, tuple[str, str, str]] = {
    "rate": ("Decimal", "rate_usd; null -> rate_fallback", "sandbox_infra"),
    "e2b_rate": ("Decimal", "Settings.e2b_sandbox_usd_per_hour", "sandbox_infra fallback"),
    "input_token_rate": ("Decimal", "default input token rate (built-in, read-only)", "llm_tokens"),
    "output_token_rate": ("Decimal", "default output token rate (built-in, read-only)", "llm_tokens"),
    "wall_clock_hours": ("Decimal", "sum of sandbox elapsed over ALL completed sandbox nodes", "sandbox_infra"),
    "tokens_input": ("int", "sum over ESTIMATED nodes (server-measured only)", "llm_tokens"),
    "tokens_output": ("int", "sum over ESTIMATED nodes (server-measured only)", "llm_tokens"),
    "tokens_estimated": ("int", "estimated-only token total (formula input)", "llm_tokens basis"),
    "tokens_input_reported": (
        "int",
        "sum of agent-reported input tokens (display-only; never a system money-math input; formula-visible)",
        "breakdown basis + operator formulas",
    ),
    "tokens_output_reported": (
        "int",
        "sum of agent-reported output tokens (display-only; never a system money-math input; formula-visible)",
        "breakdown basis + operator formulas",
    ),
    "tokens_total_reported": (
        "int",
        "sum of agent-reported total tokens (display-only; never a system money-math input; formula-visible)",
        "breakdown basis + operator formulas",
    ),
    "tokens_cache_read_reported": (
        "int",
        "sum of agent-reported cache-read tokens (display-only; never a system money-math input; formula-visible)",
        "breakdown basis + operator formulas",
    ),
    "tokens_cache_write_reported": (
        "int",
        "sum of agent-reported cache-write tokens (display-only; never a system money-math input; formula-visible)",
        "breakdown basis + operator formulas",
    ),
    "node_count": ("int", "count of completed nodes", "llm_tokens basis + operator formulas"),
    "nodes_estimated": ("int", "count of estimated nodes", "llm_tokens basis + operator formulas"),
    "reported": ("Decimal", "sum of report_key across self-reporting nodes (self_reported kind only)", "model_tokens"),
}

# Dead params — assert ABSENT (grep-asserted in tests).
# FAR-491 retired ``tokens_input_reported`` / ``tokens_output_reported`` from
# this set: they are REGISTERED now (display-only agent-reported sums).
_DEAD_PARAMS = frozenset(
    {
        "minutes_per_hour",
        "wall_clock_seconds",
        "wall_clock_minutes",
        "nodes_reported",
        "tokens_total",
    }
)

# ``calculated`` components may reference everything EXCEPT ``reported``.
CALCULATED_ALLOWED_IDENTS = frozenset(name for name in _PARAM_REGISTRY if name != "reported")
# ``self_reported`` formulas are IMPLICIT ``reported`` — the stored formula is
# NULL; validate_formula is never called for them.
SELF_REPORTED_ALLOWED_IDENTS = frozenset({"reported"})

# Map-absent nodes are NOT eligible for self-report classification (fail-safe
# toward not trusting agent-reported cost, plan §1.6). Wall-clock summing is
# deliberately NOT sandbox-gated: any node carrying a positive
# ``wall_clock_time_ms`` contributes regardless of the map (surfaced on the
# enriched union as ``is_sandbox_for_wallclock`` for downstream readers only).
_MISSING_MAP_SELF_REPORT_ELIGIBLE = False


@dataclass(frozen=True)
class CostComponentConfig:
    """Live cost-component row in the shape the engine consumes (no DB coupling)."""

    name: str
    display_name: str
    kind: str
    rate_usd: Decimal | None = None
    rate_fallback: str | None = None
    formula: str | None = None
    report_key: str | None = None
    enabled: bool = True
    sort_order: int = 0


@dataclass
class RunCostTelemetry:
    """The telemetry summary the formula engine evaluates over.

    ``wall_clock_elapsed_s`` is the INTERNAL field (never a registry
    identifier); the formula-visible identifier is ``wall_clock_hours``.
    """

    wall_clock_elapsed_s: Decimal
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_estimated: int = 0
    # Agent-reported token sums (FAR-491) — folded from the union's
    # ``reported_*`` keys. DISPLAY-ONLY: never an input to the system's
    # built-in money math (operator formulas may reference them).
    tokens_input_reported: int = 0
    tokens_output_reported: int = 0
    tokens_total_reported: int = 0
    tokens_cache_read_reported: int = 0
    tokens_cache_write_reported: int = 0
    node_count: int = 0
    nodes_estimated: int = 0
    # Count of sandbox-by-map nodes (the class that CAN self-report). Used to
    # decide whether ``missing_self_report`` is rendered (ABSENT when no
    # eligible nodes exist).
    eligible_sandbox_node_count: int = 0
    reported: dict[str, Decimal] = field(default_factory=dict)
    clamped_nodes: list[str] = field(default_factory=list)
    raw_reported: dict[str, float] = field(default_factory=dict)
    orphan_report_nodes: list[str] = field(default_factory=list)
    missing_report_keys: set[str] = field(default_factory=set)
    suspect_report_nodes: list[tuple[str, float]] = field(default_factory=list)
    per_node_cost: dict[str, Decimal] = field(default_factory=dict)


def _coerce_decimal(value: Any) -> Decimal | None:
    """Coerce a JSON-float/Decimal to Decimal via str() — never Decimal(float())."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None
    if not d.is_finite():
        return None
    return d


def _resolve_sandbox_by_map(entry: dict[str, Any]) -> bool:
    """Return whether a node is sandbox-by-map, applying the map-absent default.

    A map-absent node is NOT eligible for self-report classification (fail-safe
    toward not trusting agent-reported cost). Wall-clock summing is deliberately
    NOT sandbox-gated, so this flag only drives self-report eligibility and the
    ``eligible_sandbox_node_count`` tally.
    """
    sandbox_by_map = entry.get("sandbox_by_map") is True
    if sandbox_by_map is False and "sandbox_by_map" not in entry:
        return _MISSING_MAP_SELF_REPORT_ELIGIBLE
    return sandbox_by_map


def _classify_self_report(
    entry: dict[str, Any],
    consuming: dict[str, CostComponentConfig],
    floor: Decimal,
    sandbox_by_map: bool,
) -> CostComponentConfig | None:
    """Match a sandbox node to a consuming self_reported component by report_key.

    Returns ``None`` when the node is not self-reporting (non-sandbox, below the
    reportable floor, an unparseable/non-finite ``model_cost_usd``, or no
    consuming component matches). ``model_cost_usd`` is the fallback alias for
    any report_key.
    """
    if not sandbox_by_map:
        return None
    reported_usd = _coerce_decimal(entry.get("model_cost_usd"))
    if reported_usd is None or reported_usd < floor:
        return None
    node_report_key = entry.get("report_key")
    for rk, comp in consuming.items():
        if rk in (node_report_key, "model_cost_usd"):
            return comp
    return consuming.get("model_cost_usd") if entry.get("model_cost_usd") is not None else None


def _record_self_reported(
    telemetry: RunCostTelemetry,
    entry: dict[str, Any],
    node_id: str,
    per_node_cost: dict[str, Decimal],
    consuming_comp: CostComponentConfig,
    reported_usd: Decimal | None,
    raw_usd: Decimal | None,
) -> None:
    """Accumulate a self-reporting node: the report sum, raw/clamped markers.
    Never ``None`` in practice — ``_classify_self_report`` only matches on a
    parseable ``model_cost_usd`` — but kept optional to mirror the caller shape.
    """
    rk = consuming_comp.report_key or "model_cost_usd"
    amount = reported_usd if reported_usd is not None else Decimal(0)
    telemetry.reported[rk] = telemetry.reported.get(rk, Decimal(0)) + amount
    if raw_usd is not None:
        telemetry.raw_reported[node_id] = float(raw_usd)
    if entry.get("model_cost_clamped") is True:
        telemetry.clamped_nodes.append(node_id)
    per_node_cost[node_id] = amount


def _record_reported_tokens(telemetry: RunCostTelemetry, entry: dict[str, Any]) -> None:
    """Accumulate one node's agent-reported token usage (DISPLAY-ONLY, FAR-491).

    Sums the union's ``reported_*`` keys — validated tri-state (bool /
    non-int / negative are skipped; a valid ``0`` contributes 0). These
    counters never feed a cost calculation the system itself computes
    (operator formulas may reference them).
    """
    for union_key, counter in (
        ("reported_input_tokens", "tokens_input_reported"),
        ("reported_output_tokens", "tokens_output_reported"),
        ("reported_total_tokens", "tokens_total_reported"),
        ("reported_cache_read_tokens", "tokens_cache_read_reported"),
        ("reported_cache_write_tokens", "tokens_cache_write_reported"),
    ):
        value = entry.get(union_key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        setattr(telemetry, counter, getattr(telemetry, counter) + value)


def _record_estimated_node(
    telemetry: RunCostTelemetry,
    entry: dict[str, Any],
    node_id: str,
    per_node_cost: dict[str, Decimal],
    sandbox_by_map: bool,
    reported_usd: Decimal | None,
    raw_usd: Decimal | None,
) -> None:
    """Accumulate an estimated node: token-derived cost (server-measured only)."""
    telemetry.nodes_estimated += 1
    if sandbox_by_map and reported_usd is not None and raw_usd is not None:
        telemetry.orphan_report_nodes.append(node_id)
    in_tokens = entry.get("input_tokens") or 0
    out_tokens = entry.get("output_tokens") or 0
    telemetry.tokens_input += int(in_tokens)
    telemetry.tokens_output += int(out_tokens)
    telemetry.tokens_estimated += int(in_tokens) + int(out_tokens)
    per_node_cost[node_id] = Decimal(str(in_tokens)) * INPUT_TOKEN_RATE + Decimal(str(out_tokens)) * OUTPUT_TOKEN_RATE


def build_params(
    telemetry: RunCostTelemetry,
    component: CostComponentConfig,
    settings: Any = None,
) -> dict[str, Decimal]:
    """Build the evaluation param dict for a component from telemetry.

    ``rate`` resolves from ``component.rate_usd``; when NULL and a registered
    ``rate_fallback`` is present, the fallback value is used (currently exactly
    ``e2b_rate`` -> ``Settings.e2b_sandbox_usd_per_hour``). All values are
    Decimal-typed (ints too).
    """
    rate: Decimal | None = _coerce_decimal(component.rate_usd)
    if rate is None and component.rate_fallback == "e2b_rate" and settings is not None:
        try:
            rate = Decimal(str(settings.e2b_sandbox_usd_per_hour))
        except Exception:
            _log.warning("cost_params.e2b_rate_unavailable", exc_info=True)
            rate = None
    hours = telemetry.wall_clock_elapsed_s / Decimal(3600)

    params: dict[str, Decimal] = {
        "input_token_rate": INPUT_TOKEN_RATE,
        "output_token_rate": OUTPUT_TOKEN_RATE,
        "wall_clock_hours": hours,
        "tokens_input": Decimal(telemetry.tokens_input),
        "tokens_output": Decimal(telemetry.tokens_output),
        "tokens_estimated": Decimal(telemetry.tokens_estimated),
        "tokens_input_reported": Decimal(telemetry.tokens_input_reported),
        "tokens_output_reported": Decimal(telemetry.tokens_output_reported),
        "tokens_total_reported": Decimal(telemetry.tokens_total_reported),
        "tokens_cache_read_reported": Decimal(telemetry.tokens_cache_read_reported),
        "tokens_cache_write_reported": Decimal(telemetry.tokens_cache_write_reported),
        "node_count": Decimal(telemetry.node_count),
        "nodes_estimated": Decimal(telemetry.nodes_estimated),
    }
    if component.kind == "self_reported" and component.report_key is not None:
        params["reported"] = telemetry.reported.get(component.report_key, Decimal(0))
    if rate is not None:
        params["rate"] = rate
    return params


def _consuming_components(components: list[CostComponentConfig] | None) -> dict[str, CostComponentConfig]:
    """Index enabled self_reported components by report_key."""
    consuming: dict[str, CostComponentConfig] = {}
    for component in components or []:
        if component.enabled and component.kind == "self_reported" and component.report_key:
            consuming[component.report_key] = component
    return consuming


def _mark_missing_report_keys(telemetry: RunCostTelemetry, consuming: dict[str, CostComponentConfig]) -> None:
    """Record enabled report_keys absent from any reported node output.

    Populated regardless of the eligible sandbox node count; the eligible-node
    gate is applied at breakdown render (see aggregate.py).
    """
    reported_keys = set(telemetry.reported)
    for rk in consuming:
        if rk not in reported_keys:
            telemetry.missing_report_keys.add(rk)


def build_telemetry(
    node_token_usage: dict[str, dict[str, Any]] | None,
    components: list[CostComponentConfig] | None,
) -> tuple[RunCostTelemetry, dict[str, Decimal]]:
    """Classify every node and build the telemetry summary + per-node cost.

    ``node_token_usage`` is the ENRICHED union (per-node dicts carrying
    ``wall_clock_time_ms``, ``model_cost_usd``, ``model_cost_raw_usd``,
    ``model_cost_clamped``, ``model_cost_out_of_band_high``,
    ``is_sandbox_for_wallclock``, ``sandbox_by_map``, the SERVER token
    entries, and — FAR-491 — the agent-reported ``reported_*`` token keys).
    The union is a telemetry input ONLY in this enriched shape —
    ``outputs_json`` is never read directly.

    Returns ``(telemetry, per_node_cost)`` where ``per_node_cost`` is the
    SINGLE authority for the per-node ``cost_usd`` column.
    """
    telemetry = RunCostTelemetry(wall_clock_elapsed_s=Decimal(0))
    per_node_cost: dict[str, Decimal] = {}
    consuming = _consuming_components(components)
    entries = node_token_usage or {}
    floor = MAX_REPORTABLE_USD_MIN

    for node_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        wall_ms = entry.get("wall_clock_time_ms")
        if isinstance(wall_ms, (int, float)) and wall_ms > 0:
            telemetry.wall_clock_elapsed_s += Decimal(str(wall_ms)) / Decimal(1000)

        sandbox_by_map = _resolve_sandbox_by_map(entry)
        if sandbox_by_map:
            telemetry.eligible_sandbox_node_count += 1

        raw_usd = _coerce_decimal(entry.get("model_cost_raw_usd"))
        if raw_usd is None:
            raw_usd = _coerce_decimal(entry.get("model_cost_usd"))
        reported_usd = _coerce_decimal(entry.get("model_cost_usd"))

        consuming_comp = _classify_self_report(entry, consuming, floor, sandbox_by_map)
        if consuming_comp is None:
            _record_estimated_node(
                telemetry,
                entry,
                node_id,
                per_node_cost,
                sandbox_by_map,
                reported_usd,
                raw_usd,
            )
        else:
            _record_self_reported(telemetry, entry, node_id, per_node_cost, consuming_comp, reported_usd, raw_usd)
        _record_reported_tokens(telemetry, entry)
        telemetry.node_count += 1

    _mark_missing_report_keys(telemetry, consuming)

    return telemetry, per_node_cost
