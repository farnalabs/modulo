"""Executor finalize + ledger block (PR A2).

``finalize_cost`` is the SINGLE finalization path shared by ``execute()``,
``resume()``, the terminal handlers, and the ``request_cancellation`` cancel
path (§4.2). It:

- merges the segment sets into the stored cumulative sets (segment-wins on
  node-id collision, never summed),
- constructs the ENRICHED union (per-node cost summaries folded from the
  completed-node output dicts — the NEWLY-CONSTRUCTED consumer shape; the
  union's token fields are the SERVER entries; sandbox nodes contribute 0),
- builds the breakdown + total via ``build_telemetry`` /
  ``build_cost_breakdown`` (the single write path preserving ``total == sum``),
- persists the enriched union + merged outputs + breakdown in ONE
  ``update_run_status`` call,
- runs the TERMINAL-ONLY ledger block (``ledger_written`` /
  ``ledger_refused_at`` under ``FOR UPDATE``, bounded retry via ``begin_nested``
  savepoints for non-abort errors, whole-tx abort → the fresh-tx REDUCED
  terminalize-without-ledger escape),
- and degrades to the LEGACY FALLBACK on any cost-path exception (the
  never-fail envelope, §1.5) — persisting the UN-ENRICHED merged set with a
  wall-clock-only total that DE-TRUSTS agent ``cost_estimate_usd``.

The module is importable from both the executor (``modulo.core``) and the
route layer that owns ``request_cancellation`` (``modulo.api``); it never
imports ``modulo.api`` or ``modulo.db.crud.run``'s caller graph.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.analytics import record_run_facts
from modulo.core.cost_controller import check_and_record_spend, check_pipeline_circuit_breaker
from modulo.core.cost_controller.breakdown.aggregate import build_cost_breakdown, clamp_reported
from modulo.core.cost_controller.breakdown.constants import (
    COST_COLUMN_CAP,
    NODE_TYPE_SANDBOX_AGENT,
    TOTAL_CLAMPED_MARKER,
)
from modulo.core.cost_controller.breakdown.metrics import (
    record_duplicate_terminal,
    record_fallback_legacy,
    record_finalize_deferred,
    record_limit_refused,
    record_schema_drift,
)
from modulo.core.cost_controller.breakdown.params import (
    INPUT_TOKEN_RATE,
    OUTPUT_TOKEN_RATE,
    CostComponentConfig,
    build_telemetry,
)
from modulo.core.cost_controller.system_config import (
    acquire_kv_lock,
    read_system_config,
    write_system_config,
)
from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.core.lifecycle_map.reconcile import (
    record_journey_advance,
    record_journey_finalise_attempt,
    record_journey_parse_failure,
    record_self_report_refs_capped,
    record_unmatched_self_report_refs,
)
from modulo.core.lifecycle_map.self_report import (
    parse_self_report_refs,
    validate_and_normalise_reported_refs,
)
from modulo.core.node_output_split import (
    extend_node_type_map_from_edges,
    node_telemetry,
    split_node_output,
)
from modulo.db.crud.run import update_run_status
from modulo.db.models.agent import Agent
from modulo.db.models.cost_component import CostComponent
from modulo.db.models.journey import Journey
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import JourneyFact
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

__all__ = [
    "derive_node_type_map",
    "finalize_cancelled_run",
    "finalize_cost",
    "load_live_components",
]


class _TerminalWrite(NamedTuple):
    """Fields for the terminal (or empty) run-status write.

    Groups the scalar finalization fields that ``finalize_cost`` passes to
    ``update_run_status`` / ``_ledger_block`` / ``_write_empty_terminal``,
    cutting the argument counts of those helpers.
    """

    status: str
    error_code: str | None
    error_detail: str | None
    claim_token: str | None


class _BuiltCost(NamedTuple):
    """The cost-build outcome feeding the finalization write + ledger.

    ``total`` / ``breakdown`` are the persisted cost columns, ``enriched`` the
    ENRICHED union written back to ``node_token_usage``, and ``total_tokens``
    the derived server-measured token total.
    """

    total: Decimal
    breakdown: list[dict[str, Any]]
    enriched: dict[str, dict[str, Any]]
    total_tokens: int


class _MergedSets(NamedTuple):
    """The merged cumulative sets (segment-wins) flowing into the LEGACY FALLBACK.

    Groups ``merged_usage`` / ``merged_outputs`` / ``merged_telemetry`` — the
    three sets ``finalize_cost`` derives once and ``_fallback_write`` persists
    together, cutting that helper's argument count.
    """

    usage: dict[str, Any]
    outputs: dict[str, Any]
    telemetry: dict[str, Any]


class _JourneyResolution(NamedTuple):
    """The parsed/confirmed self-report resolution feeding journey advancement.

    Groups the raw entries, the normalised reported claims, the confirmed refs,
    the parse counters, and the merged effective refs — cutting
    ``_record_journey_outcome``'s argument count from 8 to 5.
    """

    raw: list[dict[str, Any]]
    reported: list[dict[str, Any]]
    confirmed: list[dict[str, Any]]
    counters: dict[str, int]
    effective: list[dict[str, Any]]


class _LedgerEscapeContext(NamedTuple):
    """The reduced terminalize-without-ledger escape context.

    Groups the fields ``_reduced_escape`` / ``_handle_ledger_write_failure``
    thread to a FRESH session write, cutting their argument counts.
    """

    run_id: uuid.UUID
    org_id: uuid.UUID
    status: str
    finalize_fields: dict[str, Any]
    session_factory: Callable[[], Any] | None
    claim_token: str | None


# Union JSON size guardrail — log-only, not a cap (§4.2).
_UNION_SIZE_GUARDRAIL_BYTES = 8 * 1024 * 1024

_LEGACY_E2B_RATE_DEFAULT = Decimal("0.13")

# FAR-143 journey-fact writer labels — the finalize write path that drove the
# journey hook (persisted per (run, writer) in ``modulo_journey_facts`` and
# carried as the ``writer`` metric label).
_WRITER_LIVE = "live"
_WRITER_FALLBACK = "fallback"
_WRITER_EARLY_RETURN = "early_return"

# FAR-104 — per-agent token budget enforcement (spec §4.9 / PRD error table).
_BUDGET_EXCEEDED_STATUS = "budget_exceeded"
_BUDGET_EXCEEDED_ERROR_CODE = "budget_exceeded"
_BUDGET_EXCEEDED_MESSAGE = "This run exceeded its token budget."


def _e2b_rate() -> Decimal:
    """The E2B hourly rate for the LEGACY FALLBACK's wall-clock cost (runtime read)."""
    try:
        from modulo.settings import get_settings

        return Decimal(str(get_settings().e2b_sandbox_usd_per_hour))
    except Exception:
        return _LEGACY_E2B_RATE_DEFAULT


def _merge(stored: Any, segment: Any, *, segment_wins: bool = True) -> dict[str, Any]:
    """Merge two per-node dicts; on node-id collision the SEGMENT wins.

    Both *stored* and *segment* may be ``None``/not-dict (a ``None`` segment is
    an empty accumulator — ``{}``/``None`` normalize so the stored set is
    untouched). Always returns a fresh dict.
    """
    merged: dict[str, Any] = {}
    if isinstance(stored, dict):
        merged.update(stored)
    if not isinstance(segment, dict):
        return merged
    for node_id, value in segment.items():
        if segment_wins:
            merged[node_id] = value
        else:
            merged.setdefault(node_id, value)
    return merged


_RECOVERY_TELEMETRY_FIELDS = ("recovered", "recovery_input")


def _preserve_recovery_fields(stored_entry: Any, telemetry: dict[str, Any]) -> None:
    """Recovery-vs-finalize: NEVER clobber a node's stored recovery telemetry.

    When a node's stored telemetry carries ``recovered`` / ``recovery_input``
    and the freshly split telemetry (from a segment value that has moved past
    the recovery marker) lacks them, fold the stored fields in. The
    already-pure idempotence branch never reaches this (the stored entry IS the
    telemetry); it guards the re-split branch so a later finalize merge keeps
    recovery facts instead of overwriting them.
    """
    if not isinstance(stored_entry, dict):
        return
    for key in _RECOVERY_TELEMETRY_FIELDS:
        if key in stored_entry and key not in telemetry:
            telemetry[key] = stored_entry[key]


def _log_output_resplit(run_id: str | None, node_id: str) -> None:
    """Log a LEGACY row being re-split (FAR-125 P1b) — observable migration signal."""
    _log.info("cost_finalize.legacy_output_resplit", extra={"run_id": run_id, "node_id": node_id})


def _split_merge_outputs(
    stored_outputs: Any,
    stored_telemetry: Any,
    segment: Any,
    node_type_map: dict[str, str],
    *,
    run_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split-then-merge a segment into the two LOCKSTEP output columns.

    For EVERY node in *segment* the value is routed through
    ``node_output_split.split_node_output``:
      - an already-pure node (a stored telemetry entry exists) is an
        IDEMPOTENT NO-OP — the return and the stored telemetry pass through
        unchanged (``stored_telemetry`` is the P1 split signal);
      - a legacy row is RE-SPLIT (logged with run_id + node_id) into its pure
        return + exhaustive telemetry.

    The pure returns land in the ``outputs_json`` merge and the telemetry in
    the parallel ``node_telemetry_json`` merge — LOCKSTEP: every
    ``outputs_json`` key is guaranteed a telemetry key (``{}`` at minimum). A
    skipped recovery marker is the sole exception: its ``outputs_json`` key is
    OMITTED (the telemetry entry is the sole record). Stored rows absent from
    the segment carry over verbatim (legacy stored rows re-split so lockstep
    holds). Segment collisions are segment-wins, never summed.

    NEVER raises: ``split_node_output`` is never-raises and malformed rows
    degrade to best-effort splits.
    """
    merged_outputs: dict[str, Any] = {}
    merged_telemetry: dict[str, Any] = {}

    stored_out = stored_outputs if isinstance(stored_outputs, dict) else {}
    stored_tel = stored_telemetry if isinstance(stored_telemetry, dict) else {}

    _merge_stored_outputs(merged_outputs, merged_telemetry, stored_out, stored_tel, node_type_map, run_id)
    _merge_segment_outputs(merged_outputs, merged_telemetry, segment, stored_tel, node_type_map, run_id)

    return merged_outputs, merged_telemetry


def _merge_stored_outputs(
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    stored_out: dict[str, Any],
    stored_tel: dict[str, Any],
    node_type_map: dict[str, str],
    run_id: str | None,
) -> None:
    """Split-then-merge every stored (already-persisted) output row."""
    for node_id, value in stored_out.items():
        _merge_stored_output(merged_outputs, merged_telemetry, str(node_id), value, stored_tel, node_type_map, run_id)


def _merge_segment_outputs(
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    segment: Any,
    stored_tel: dict[str, Any],
    node_type_map: dict[str, str],
    run_id: str | None,
) -> None:
    """Split-then-merge every segment output row (fresh split)."""
    if not isinstance(segment, dict):
        return
    for node_id, seg_value in segment.items():
        _merge_segment_output(
            merged_outputs,
            merged_telemetry,
            str(node_id),
            seg_value,
            stored_tel,
            node_type_map,
            run_id,
        )


def _store_split_result(
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    node_id: str,
    ret: Any,
    telemetry: dict[str, Any],
) -> None:
    """Store one split result into the two LOCKSTEP columns.

    The outputs key is omitted when the split is a skipped-recovery marker
    (``ret is None and telemetry.skipped``); the telemetry entry is the sole
    record. Lockstep is otherwise strict: every stored output key gets a
    telemetry key.
    """
    if not (ret is None and telemetry.get("skipped") is True):
        merged_outputs[node_id] = ret
    merged_telemetry[node_id] = telemetry


def _merge_stored_output(
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    node_id: str,
    value: Any,
    stored_tel: dict[str, Any],
    node_type_map: dict[str, str],
    run_id: str | None,
) -> None:
    """Split-then-merge ONE stored (already-persisted) output row."""
    if node_id in stored_tel:
        merged_outputs[node_id] = value
        merged_telemetry[node_id] = stored_tel[node_id]
        return
    _log_output_resplit(run_id, node_id)
    ret, telemetry = split_node_output(value, node_type_map.get(node_id, ""), None, run_id=run_id, node_id=node_id)
    _store_split_result(merged_outputs, merged_telemetry, node_id, ret, telemetry)


def _merge_segment_output(
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    node_id: str,
    seg_value: Any,
    stored_tel: dict[str, Any],
    node_type_map: dict[str, str],
    run_id: str | None,
) -> None:
    """Split-then-merge ONE segment output row (fresh split against the stored signal)."""
    stored_entry = stored_tel.get(node_id)
    ret, telemetry = split_node_output(
        seg_value,
        node_type_map.get(node_id, ""),
        stored_entry,
        run_id=run_id,
        node_id=node_id,
    )
    if stored_entry is None:
        _log_output_resplit(run_id, node_id)
    _preserve_recovery_fields(stored_entry, telemetry)
    _store_split_result(merged_outputs, merged_telemetry, node_id, ret, telemetry)


def _node_output_dict(merged_outputs: Any, node_id: str, merged_telemetry: Any = None) -> dict[str, Any] | None:
    """The inner ``output`` dict of a completed node (or ``None``).

    Routes through ``node_output_split.node_telemetry`` so the legacy
    extraction is SHARED and identical everywhere (FAR-124 P0). When a split
    telemetry entry exists (FAR-125 P1 rows) it is returned verbatim; ``None``
    here means "no telemetry entry", which selects the legacy-row branch that
    mirrors the historical implementation exactly.
    """
    value = node_telemetry(merged_telemetry, merged_outputs, node_id)
    return value if isinstance(value, dict) else None


def _pop_model_cost_fields(node_dict: dict[str, Any]) -> None:
    for key in (
        "model_cost_usd",
        "model_cost_raw_usd",
        "model_cost_clamped",
        "model_cost_out_of_band_high",
    ):
        node_dict.pop(key, None)


def _fold_stored_clamped(node_dict: dict[str, Any]) -> None:
    """Branch (3): output ABSENT — re-clamp the stored-union value (fallback authority).

    The stored ``model_cost_usd`` is re-validated through ``clamp_reported`` and
    the folded flags derive from the re-clamped fold.
    """
    stored = node_dict.get("model_cost_usd")
    if stored is None:
        return
    folded = clamp_reported(stored)
    if folded is None:
        _pop_model_cost_fields(node_dict)
        return
    clamped_val, _was_clamped, oob = folded
    node_dict["model_cost_usd"] = float(clamped_val)
    node_dict["model_cost_clamped"] = bool(node_dict.get("model_cost_clamped", _was_clamped))
    node_dict["model_cost_out_of_band_high"] = bool(node_dict.get("model_cost_out_of_band_high", oob))


def _fold_from_output_obj(node_dict: dict[str, Any], output_obj: dict[str, Any]) -> None:
    """Branch (1): output PRESENT + carries ``model_cost_usd`` → overwrite with the
    re-clamped fold (the FULL mirror of the extraction validation, defense-in-depth;
    the input is the RAW field when present, else the clamped value — the
    explicit-None pin)."""
    raw_field = output_obj.get("model_cost_raw_usd")
    fold_input = raw_field if raw_field is not None else output_obj.get("model_cost_usd")
    if fold_input is None:
        _pop_model_cost_fields(node_dict)
        return
    folded = clamp_reported(fold_input)
    if folded is None:
        _pop_model_cost_fields(node_dict)
        return
    clamped_val, _was_clamped, _oob = folded
    node_dict["model_cost_usd"] = float(clamped_val)
    if raw_field is not None:
        node_dict["model_cost_raw_usd"] = float(raw_field)
    else:
        node_dict.pop("model_cost_raw_usd", None)
    node_dict["model_cost_clamped"] = bool(output_obj.get("model_cost_clamped", _was_clamped))
    node_dict["model_cost_out_of_band_high"] = bool(output_obj.get("model_cost_out_of_band_high", _oob))


def _fold_model_cost(node_dict: dict[str, Any], output_obj: dict[str, Any] | None) -> None:
    """The PINNED stored-union ONE-mechanism rule for ``model_cost_usd`` (§4.2/§4.5).

    (1) output PRESENT + carries ``model_cost_usd`` → OVERWRITE with the
        re-clamped fold (``_fold_from_output_obj``);
    (2) output PRESENT but LACKS ``model_cost_usd`` → pop the value + sibling
        flags (the node is estimated);
    (3) output ABSENT from both stored ``outputs_json`` and the current segment
        → the stored-union value is re-clamped through ``clamp_reported``
        (``_fold_stored_clamped`` — fallback authority, the third-path class).

    ``model_cost_clamped`` / ``model_cost_out_of_band_high`` are the
    AUTHORITATIVE values folded from the node-output dict written by
    extraction; ``clamp_reported``'s own flags are the fallback only when the
    output lacks them.
    """
    if output_obj is None:
        _fold_stored_clamped(node_dict)
        return
    if "model_cost_usd" in output_obj:
        _fold_from_output_obj(node_dict, output_obj)
        return
    _pop_model_cost_fields(node_dict)


def _enrich_union(
    merged_usage: dict[str, Any],
    merged_outputs: dict[str, Any],
    node_type_map: dict[str, str],
    is_terminal: bool = False,
    merged_telemetry: Any = None,
) -> dict[str, dict[str, Any]]:
    """Fold per-node cost summaries from the completed-node output dicts into
    the union BEFORE ``build_telemetry`` (§4.2).

    The union is NEWLY CONSTRUCTED here: the union's token fields
    (``input_tokens``/``output_tokens``/``total_tokens``) are the SERVER
    entries from ``node_token_usage``; sandbox nodes contribute 0. Agent
    ``token_usage`` is never folded in (v22 M1). The SPLIT sandbox signal is
    set from the run-frozen node-type map, NOT field presence.

    Per-node telemetry is read from the split ``node_telemetry_json`` column
    when present (FAR-125 P1b); legacy rows fall back to the shared
    ``node_telemetry`` extraction so the enriched shape is identical either
    way.

    The SCHEMA-DRIFT counter increment happens here (the frozen map is in
    scope) and is TERMINAL-ONLY, gated on ``pin_failed == false`` AND the node
    being sandbox-by-map (provenance gate). The map completeness + a
    type-distribution ratio are logged so a systemic map-drift is observable.
    """
    union: dict[str, dict[str, Any]] = {}
    if isinstance(merged_usage, dict):
        for node_id, usage in merged_usage.items():
            nid = str(node_id)
            if isinstance(usage, dict):
                union[nid] = dict(usage)
            else:
                union[nid] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if isinstance(merged_outputs, dict):
        for node_id in merged_outputs:
            union.setdefault(str(node_id), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

    missing_node_type: list[str] = []
    executed_types: Counter[str] = Counter()

    for node_id, node_dict in union.items():
        output_obj = _node_output_dict(merged_outputs, node_id, merged_telemetry)
        map_type = _enrich_node_fields(node_dict, output_obj, node_type_map.get(node_id))
        if output_obj is not None:
            executed_types[map_type or "<map_absent>"] += 1
            if map_type is None:
                missing_node_type.append(node_id)
            if is_terminal:
                _record_node_schema_drift(output_obj, map_type)

    _log_node_type_summary(missing_node_type, executed_types)

    return union


def _log_node_type_summary(missing_node_type: list[str], executed_types: Counter[str]) -> None:
    """Log the schema-drift provenance map-completeness + type distribution."""
    if missing_node_type:
        _log.warning("cost_components_missing_node_type", extra={"node_ids": missing_node_type})
    if executed_types:
        _log.info("cost_components_node_type_ratio", extra={"executed_types": dict(executed_types)})


def _wall_clock_ms(output_obj: Any) -> int | float | None:
    """The server-verified wall-clock duration (ms) of a node-output dict."""
    if isinstance(output_obj, dict):
        value = output_obj.get("wall_clock_time_ms")
        if isinstance(value, (int, float)):
            return value
    return None


def _enrich_node_fields(
    node_dict: dict[str, Any],
    output_obj: dict[str, Any] | None,
    map_type: str | None,
) -> str | None:
    """Stamp one union entry's derived fields from its node-output dict.

    Sets ``wall_clock_time_ms`` (server-verified when present), the split
    sandbox flags (``sandbox_by_map`` / ``is_sandbox_for_wallclock``) and the
    pinned model-cost fold. Returns the node's mapped type (``None`` when the
    frozen map lacks the node — the schema-drift provenance gate).
    """
    wall_ms = _wall_clock_ms(output_obj)
    if wall_ms is not None:
        node_dict["wall_clock_time_ms"] = wall_ms

    node_dict["sandbox_by_map"] = map_type == NODE_TYPE_SANDBOX_AGENT
    node_dict["is_sandbox_for_wallclock"] = (map_type == NODE_TYPE_SANDBOX_AGENT) or (
        map_type is None and wall_ms is not None
    )

    _fold_model_cost(node_dict, output_obj)
    return map_type


def _record_node_schema_drift(output_obj: dict[str, Any], map_type: str | None) -> None:
    """TERMINAL-ONLY schema-drift counter, gated on pin_failed + sandbox-by-map."""
    schema_drift = output_obj.get("schema_drift")
    if schema_drift and output_obj.get("pin_failed") is not True and map_type == NODE_TYPE_SANDBOX_AGENT:
        record_schema_drift()


def _write_back_node_cost(
    enriched: dict[str, dict[str, Any]],
    per_node_cost: dict[str, Decimal],
) -> dict[str, dict[str, Any]]:
    """Populate the union's per-node ``cost_usd`` from the SINGLE authority.

    ``per_node_cost`` is computed inside ``build_telemetry``; writing it back
    here guarantees the union's ``cost_usd`` and the breakdown/telemetry NEVER
    disagree (an orphan-report node's ``cost_usd`` is token-derived, never its
    ``model_cost_usd``).
    """
    for node_id, cost in per_node_cost.items():
        entry = enriched.setdefault(str(node_id), {})
        entry["cost_usd"] = float(cost)
    return enriched


def _derive_total_tokens(enriched: dict[str, dict[str, Any]]) -> int:
    """Derive ``Run.total_tokens`` from the SERVER-measured entries only (v22 M1)."""
    total = 0
    for entry in (enriched or {}).values():
        if not isinstance(entry, dict):
            continue
        tt = entry.get("total_tokens")
        if isinstance(tt, (int, float)) and not isinstance(tt, bool):
            total += int(tt)
        else:
            total += int(entry.get("input_tokens") or 0) + int(entry.get("output_tokens") or 0)
    return total


def derive_node_type_map(graph_json: Any) -> dict[str, str]:
    """Derive ``{node_id: node_type}`` from a snapshot's ``graph_json``.

    The map is FROZEN at run start (§1.6) and passed into ``finalize_cost`` at
    every pause and resume — never re-read from a mutable store at resume. The
    graph is immutable per snapshot, so deriving from ``graph_json`` at any
    point yields the same map.
    """
    result: dict[str, str] = {}
    if not isinstance(graph_json, dict):
        return result
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return result
    for node in nodes:
        if isinstance(node, dict) and node.get("id"):
            result[str(node["id"])] = node.get("node_type") or ""
    return result


def derive_node_agent_map(graph_json: Any) -> dict[str, str]:
    """Derive ``{node_id: agent_id}`` from a snapshot's ``graph_json``.

    Edge-synthesized HITL gate nodes carry no ``agent_id`` and are simply
    absent from the map (they never contribute agent-scoped token usage).
    """
    result: dict[str, str] = {}
    if not isinstance(graph_json, dict):
        return result
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return result
    for node in nodes:
        if isinstance(node, dict) and node.get("id") and node.get("agent_id"):
            result[str(node["id"])] = str(node["agent_id"])
    return result


def _accumulate_agent_tokens(
    usage: dict[str, Any] | None,
    node_agent_map: dict[str, str],
) -> dict[str, int]:
    """Sum per-agent accumulated tokens across the run's nodes (FAR-104).

    Tokens are the SERVER-measured entries from the enriched union — the same
    authority ``_derive_total_tokens`` uses. Each node's ``total_tokens`` is
    attributed to its node's agent (nodes without an ``agent_id`` — HITL gates,
    sandbox-only nodes — contribute nothing). Returns ``{agent_id: tokens}``.
    """
    per_agent: dict[str, int] = {}
    for node_id, entry in (usage or {}).items():
        agent_id = node_agent_map.get(str(node_id))
        if agent_id is None:
            continue
        if not isinstance(entry, dict):
            continue
        tt = entry.get("total_tokens")
        if isinstance(tt, (int, float)) and not isinstance(tt, bool):
            tokens = int(tt)
        else:
            tokens = int(entry.get("input_tokens") or 0) + int(entry.get("output_tokens") or 0)
        per_agent[agent_id] = per_agent.get(agent_id, 0) + tokens
    return per_agent


async def load_live_components(session: AsyncSession, org_id: uuid.UUID) -> list[CostComponentConfig]:
    """Read LIVE enabled, non-deleted component rows in-transaction (§1.4)."""
    result = await session.execute(
        select(CostComponent)
        .where(
            CostComponent.organisation_id == org_id,
            CostComponent.deleted_at.is_(None),
        )
        .order_by(CostComponent.sort_order, CostComponent.name)
    )
    rows = list(result.scalars().all())
    return [
        CostComponentConfig(
            name=r.name,
            display_name=r.display_name,
            kind=r.kind,
            rate_usd=r.rate_usd,
            rate_fallback=r.rate_fallback,
            formula=r.formula,
            report_key=r.report_key,
            enabled=r.enabled,
            sort_order=r.sort_order,
        )
        for r in rows
    ]


def _token_cost(merged_usage: dict[str, Any]) -> Decimal:
    """Legacy-fallback token cost from the SERVER token entries (constant rates)."""
    total = Decimal(0)
    for entry in (merged_usage or {}).values():
        if not isinstance(entry, dict):
            continue
        in_tokens = int(entry.get("input_tokens") or 0)
        out_tokens = int(entry.get("output_tokens") or 0)
        total += Decimal(str(in_tokens)) * INPUT_TOKEN_RATE
        total += Decimal(str(out_tokens)) * OUTPUT_TOKEN_RATE
    return total


def _legacy_sandbox_cost(merged_outputs: dict[str, Any], merged_telemetry: Any = None) -> Decimal:
    """Legacy-fallback sandbox cost — SERVER-VERIFIED WALL-CLOCK ONLY.

    ``elapsed/3600 * E2B_SANDBOX_USD_PER_HOUR`` over all completed sandbox
    nodes. The fallback DE-TRUSTS agent ``cost_estimate_usd`` (§1.5) — a hostile
    legacy ``cost_estimate_usd`` can no longer inflate the fallback total.
    Per-node telemetry is read from the split ``node_telemetry_json`` column
    when present (FAR-125 P1b) with the legacy extraction fallback.
    """
    if not isinstance(merged_outputs, dict):
        return Decimal(0)
    total = Decimal(0)
    rate = _e2b_rate()
    for node_id in merged_outputs:
        out = node_telemetry(merged_telemetry, merged_outputs, node_id)
        if not isinstance(out, dict):
            continue
        wall_ms = out.get("wall_clock_time_ms")
        if isinstance(wall_ms, (int, float)) and wall_ms > 0 and math.isfinite(wall_ms):
            total += (Decimal(str(wall_ms)) / Decimal(3600000)) * rate
    return total


def _entry_amount(amount: Decimal) -> str:
    """6dp string, string-clamped to the flat ceiling (never ``1E+40``)."""
    return format(min(amount, COST_COLUMN_CAP), "f")


async def _fallback_write(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    merged: _MergedSets,
    error_code: str | None,
    error_detail: str | None,
    is_terminal: bool = False,
    claim_token: str | None = None,
) -> None:
    """The LEGACY FALLBACK write (never-fail envelope, §1.5).

    Persists the UN-ENRICHED merged set (so the cumulative write-back invariant
    survives a cost-path exception) with ``total = token_cost +
    legacy_sandbox_cost`` — wall-clock ONLY, flat-clamped with the shared
    ``total_clamped`` marker. The two output columns are written SHAPE-IDENTICAL
    to the main path (FAR-125 P1b): ``outputs_json`` holds the PURE returns and
    ``node_telemetry_json`` the split telemetry — never an un-split envelope.
    On a terminal write the analytics fact is recorded in the SAME transaction
    (fail-open, ADR 020).
    """
    total_tokens = _derive_total_tokens(merged.usage)
    token_cost = _token_cost(merged.usage)
    sandbox_cost = _legacy_sandbox_cost(merged.outputs, merged.telemetry)
    total = token_cost + sandbox_cost
    if not total.is_finite():
        total = Decimal(0)
    wall_hours = _fallback_wall_hours(merged.outputs, merged.telemetry)
    breakdown = _build_fallback_breakdown(token_cost, sandbox_cost, wall_hours, merged)
    if total > COST_COLUMN_CAP:
        total = COST_COLUMN_CAP
        breakdown.insert(0, dict(TOTAL_CLAMPED_MARKER))
    await update_run_status(
        session,
        run_id,
        status,
        error_code=error_code,
        error_detail=error_detail,
        total_cost_usd=total,
        cost_breakdown=breakdown,
        node_token_usage=merged.usage,
        outputs_json=merged.outputs,
        node_telemetry_json=merged.telemetry,
        total_tokens=total_tokens,
        claim_token=claim_token,
    )
    if is_terminal:
        await _record_fallback_terminal_facts(session, run_id, status, merged.outputs)


def _fallback_wall_hours(merged_outputs: dict[str, Any], merged_telemetry: Any) -> float:
    """Server-verified wall-clock hours across completed nodes (legacy fallback)."""
    wall_hours = 0.0
    if isinstance(merged_outputs, dict):
        for node_id in merged_outputs:
            out = node_telemetry(merged_telemetry, merged_outputs, node_id)
            if isinstance(out, dict) and isinstance(out.get("wall_clock_time_ms"), (int, float)):
                wall_hours += float(out["wall_clock_time_ms"]) / 3600000.0
    return wall_hours


def _build_fallback_breakdown(
    token_cost: Decimal,
    sandbox_cost: Decimal,
    wall_hours: float,
    merged: _MergedSets,
) -> list[dict[str, Any]]:
    """The LEGACY FALLBACK breakdown — LLM tokens + sandbox infra entries."""
    return [
        {
            "component": "llm_tokens",
            "display_name": "LLM Tokens",
            "source": "calculated",
            "amount_usd": _entry_amount(token_cost),
            "formula_applied": ("tokens_input * input_token_rate + tokens_output * output_token_rate"),
            "rate_usd": None,
            "basis": {
                "tokens_input": _usage_token_sum(merged.usage, "input_tokens"),
                "tokens_output": _usage_token_sum(merged.usage, "output_tokens"),
                "nodes_estimated": 0,
            },
        },
        {
            "component": "sandbox_infra",
            "display_name": "Sandbox Infrastructure",
            "source": "calculated",
            "amount_usd": _entry_amount(sandbox_cost),
            "formula_applied": "rate * wall_clock_hours",
            "rate_usd": str(_e2b_rate()),
            "basis": {"wall_clock_hours": wall_hours},
        },
    ]


def _usage_token_sum(merged_usage: dict[str, Any], key: str) -> int:
    """Sum one token field across the SERVER usage entries (dicts only)."""
    return int(sum((e.get(key) or 0) for e in (merged_usage or {}).values() if isinstance(e, dict)))


async def _record_fallback_terminal_facts(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    merged_outputs: dict[str, Any],
) -> None:
    """FAR-143 — the LEGACY FALLBACK terminal also records facts + advances journeys."""
    run = await session.get(Run, run_id)
    if run is not None:
        await record_run_facts(session, run)
        # FAR-143 — the LEGACY FALLBACK terminal also advances journeys
        # (fail-open, own savepoint).
        await _advance_journeys_on_terminal(session, run, status, merged_outputs, writer=_WRITER_FALLBACK)


def _is_abort_error(exc: Exception) -> bool:
    """True for a whole-tx abort (deadlock / serialization failure).

    A whole-tx abort must go STRAIGHT to the reduced escape (retrying a
    savepoint inside an aborted transaction is pointless). Detected portably by
    the DBAPI error class name so non-Postgres backends behave identically.
    """
    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    if orig is None:
        return False
    return type(orig).__name__ in {
        "DeadlockDetectedError",
        "SerializationError",
        "SerializationFailure",
        "LockNotAvailableError",
    }


async def _record_ledger_with_retry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    cost_usd: Decimal,
    team_id: uuid.UUID | None,
    run_id: uuid.UUID,
    run_date: date,
    attempts: int = 3,
) -> tuple[bool, str | None]:
    """Record the terminal spend with BOUNDED RETRY (``begin_nested`` savepoints).

    Non-abort failures roll the savepoint back and retry; a whole-tx abort
    re-raises (the caller runs the reduced escape). ``(False,
    "daily_limit_exceeded")`` is a clean return (a PERMANENT refusal, NOT a
    failure — the refused amount was already persisted by
    ``check_and_record_spend``).
    """
    last_reason: str | None = None
    for attempt in range(attempts):
        savepoint = await session.begin_nested()
        try:
            ok, reason = await check_and_record_spend(
                session,
                org_id=org_id,
                cost_usd=cost_usd,
                team_id=team_id,
                run_id=run_id,
                run_date=run_date,
            )
        except asyncio.CancelledError:
            await savepoint.rollback()
            raise
        except Exception as exc:
            await savepoint.rollback()
            if _is_abort_error(exc):
                raise
            last_reason = type(exc).__name__
            _log.warning(
                "cost_ledger.write_retry",
                extra={"run_id": str(run_id), "attempt": attempt + 1, "exc_type": type(exc).__name__},
            )
            continue
        await savepoint.commit()
        return ok, reason
    _log.error(
        "cost_ledger.write_failed",
        extra={"run_id": str(run_id), "reason": last_reason or "write_failure"},
    )
    return False, last_reason or "write_failure"


async def _reduced_escape(
    _session: AsyncSession,
    ctx: _LedgerEscapeContext,
) -> None:
    """The REDUCED terminalize-without-ledger escape (§4.2).

    Persists the FULL finalization field set in a FRESH transaction, sets
    NOTHING ELSE, leaves ``ledger_written = false``. Engages ONLY for genuine
    write failures, never a ``daily_limit_exceeded`` refusal. The status write
    is fenced by *claim_token* (a superseded executor's escape is a no-op).
    """
    if ctx.session_factory is None:
        _log.error("cost_ledger.reduced_escape_unavailable", extra={"run_id": str(ctx.run_id)})
        return
    try:
        async with ctx.session_factory() as fresh, fresh.begin():
            await set_rls_org(fresh, ctx.org_id)
            run = await update_run_status(
                fresh,
                ctx.run_id,
                ctx.status,
                **ctx.finalize_fields,
                claim_token=ctx.claim_token,
            )
            if run is not None:
                await record_run_facts(fresh, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_ledger.reduced_escape_failed", extra={"run_id": str(ctx.run_id)})


async def _ledger_block(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    status: str,
    total: Decimal,
    owner_team_id: uuid.UUID | None,
    run_date: date,
    finalize_fields: dict[str, Any],
    session_factory: Callable[[], Any] | None,
    claim_token: str | None = None,
) -> None:
    """Terminal-only ledger block — guarded, retried, then the reduced escape.

    The duplicate-terminal guard is ``ledger_written OR ledger_refused_at IS
    NOT NULL`` under ``FOR UPDATE``; nothing clears ``ledger_refused_at``, so a
    refused run stays out of the ledger PERMANENTLY. A limit-refused terminal
    sets ``ledger_refused_at`` + ``limit_refused{team}`` (the refused amount is
    already persisted by ``check_and_record_spend``); a write failure runs the
    reduced escape with ``finalize_deferred{reason="write_failure", team}``.
    """
    locked = (await session.execute(select(Run).where(Run.id == run_id).with_for_update())).scalar_one()
    if locked.ledger_written or locked.ledger_refused_at is not None:
        _log.warning("cost_ledger.duplicate_terminal", extra={"run_id": str(run_id)})
        record_duplicate_terminal()
        await _record_duplicate_terminal_event(session, run_id)
        return

    try:
        ok, reason = await _record_ledger_with_retry(
            session,
            org_id=org_id,
            cost_usd=total,
            team_id=owner_team_id,
            run_id=run_id,
            run_date=run_date,
            attempts=3,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            "cost_ledger.whole_tx_abort",
            extra={"run_id": str(run_id), "exc_type": type(exc).__name__},
        )
        ok, reason = False, "whole_tx_abort"

    if _is_limit_refused(ok, reason):
        await _handle_limit_refused(session, locked, run_id, owner_team_id)
        return

    if not ok:
        await _handle_ledger_write_failure(
            session,
            _LedgerEscapeContext(
                run_id=run_id,
                org_id=org_id,
                status=status,
                finalize_fields=finalize_fields,
                session_factory=session_factory,
                claim_token=claim_token,
            ),
            owner_team_id,
            reason,
        )
        return

    locked.ledger_written = True
    await session.flush()
    await _check_circuit_breaker(session, locked, org_id, total, run_id)


async def _handle_limit_refused(
    session: AsyncSession,
    locked: Run,
    run_id: uuid.UUID,
    owner_team_id: uuid.UUID | None,
) -> None:
    """LIMIT-REFUSED — expected healthy enforcement, NOT a ledger failure."""
    locked.ledger_refused_at = datetime.now(UTC)
    record_limit_refused(str(owner_team_id or "none"))
    _log.info("cost_ledger.limit_reached", extra={"run_id": str(run_id)})
    await session.flush()


async def _handle_ledger_write_failure(
    session: AsyncSession,
    ctx: _LedgerEscapeContext,
    owner_team_id: uuid.UUID | None,
    reason: str | None,
) -> None:
    """REDUCED terminalize-without-ledger escape, write_failure ONLY."""
    _log.error(
        "cost_ledger.finalize_deferred",
        extra={"reason": reason or "unknown", "run_id": str(ctx.run_id)},
    )
    record_finalize_deferred(reason="write_failure", team=str(owner_team_id or "none"))
    await _reduced_escape(session, ctx)


def _is_limit_refused(ok: bool, reason: str | None) -> bool:
    """True for a PERMANENT daily-limit refusal — a clean return, NOT a failure.

    The refused amount was already persisted by ``check_and_record_spend``.
    """
    return not ok and reason is not None and reason.startswith("daily_limit_exceeded")


async def _check_circuit_breaker(
    session: AsyncSession,
    locked: Run,
    org_id: uuid.UUID,
    total: Decimal,
    run_id: uuid.UUID,
) -> None:
    """FAR-105 cost-control circuit breaker — best-effort monthly-spend trip.

    The monthly sum EXCLUDES the current run (its cost is already persisted and
    is added back as ``total``). FAIL-OPEN: a breaker failure must never fail
    the terminal write (the ledger is the enforcement; the trip is
    best-effort control).
    """
    try:
        await check_pipeline_circuit_breaker(
            session,
            org_id=org_id,
            pipeline_id=locked.pipeline_id,
            cost_usd=total,
            run_id=run_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "cost_ledger.circuit_breaker_check_failed",
            extra={"run_id": str(run_id), "pipeline_id": str(locked.pipeline_id)},
        )


async def _record_duplicate_terminal_event(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Record a duplicate-terminal event for the probe's FLOOD trigger (§4.7).

    The event rides in a bounded ``duplicate_terminal_events`` list on the
    GLOBAL ``system_config`` (NO RLS — the same discipline as ``probe_state``),
    under the advisory-lock read-modify-write. The probe counts DISTINCT
    run-ids within the 10-minute window; a stale event log is harmless (the
    probe trims on read). Never raises — a duplicate guard firing must not fail
    the terminal path.
    """
    key = "duplicate_terminal_events"
    try:
        await acquire_kv_lock(session, key)
        events = await read_system_config(session, key) or []
        if not isinstance(events, list):
            events = []
        events.append({"run_id": str(run_id), "ts": datetime.now(UTC).isoformat()})
        kept = _trim_duplicate_events(events)
        await write_system_config(session, key, kept)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_ledger.duplicate_event_record_failed", extra={"run_id": str(run_id)})


def _trim_duplicate_events(events: list[Any]) -> list[dict[str, Any]]:
    """Keep the most recent 100 events, dropping stale (>10 min) entries.

    Only the last 200 events are inspected; unparseable timestamps are kept
    (a stale event log is harmless — the probe trims on read).
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=10 * 60)
    kept: list[dict[str, Any]] = []
    for event in events[-200:]:
        ts = event.get("ts")
        try:
            parsed = datetime.fromisoformat(ts) if ts else None
        except (ValueError, TypeError):
            parsed = None
        if parsed is None or parsed >= cutoff:
            kept.append(event)
    return kept[-100:]


async def _confirm_reported_refs(
    session: AsyncSession,
    org_id: uuid.UUID,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Self-report is ADVISORY — keep only refs with an existing journey row.

    A reported claim (``source="reported"``) can only CONFIRM / MATCH an
    existing journey keyed by the same canonical ``(org, kind, ref)``; it can
    NEVER mint one (minting is owned by the create-time
    ``INSERT ... ON CONFLICT DO NOTHING`` path in ``modulo.db.crud.run``).
    ``entries`` are already canonicalised (kind/ref/source="reported") by
    ``validate_and_normalise_reported_refs``. Org-scoped SELECT EXISTS per
    entry — the caller owns RLS context.
    """
    confirmed: list[dict[str, Any]] = []
    for entry in entries:
        exists = (
            await session.execute(
                select(Journey.id).where(
                    Journey.organisation_id == org_id,
                    Journey.kind == entry["kind"],
                    Journey.ref == entry["ref"],
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            confirmed.append(entry)
    return confirmed


def _merge_effective_refs(
    stamped: list[dict[str, Any]] | None,
    confirmed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create-stamped refs first, then confirmed reported refs (dedup, cap 100).

    A reported (kind, ref) that duplicates a create-stamped entry is collapsed
    (first occurrence wins — the derived stamp predates the reported claim). The
    combined list is capped at 100 so a hostile output cannot grow the run's
    ``work_item_refs`` without bound.
    """
    effective: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in stamped or []:
        if isinstance(entry, dict) and entry.get("kind") and entry.get("ref"):
            key = (str(entry["kind"]), str(entry["ref"]))
            if key not in seen:
                seen.add(key)
                effective.append(entry)
    for entry in confirmed:
        key = (entry["kind"], entry["ref"])
        if key not in seen:
            seen.add(key)
            effective.append(entry)
    return effective[:100]


async def _record_journey_fact(
    session: AsyncSession,
    run: Run,
    writer: str,
    parse_failures: int,
    finalise_attempts: int,
) -> None:
    """Persist the per-writer journey denominators (``modulo_journey_facts``).

    FAIL-OPEN in its own savepoint: a fact-write failure must never fail the
    journey advance (outer savepoint) or the terminal write. The upsert is
    ``INSERT ... ON CONFLICT (run_id, writer) DO UPDATE`` — a re-finalization
    corrects the fact in place (idempotent).
    """
    try:
        async with session.begin_nested():
            stmt = pg_insert(JourneyFact).values(
                id=uuid.uuid4(),
                organisation_id=run.organisation_id,
                run_id=run.id,
                writer=writer,
                parse_failures=parse_failures,
                finalise_attempts=finalise_attempts,
            )
            update_cols = {
                "parse_failures": stmt.excluded.parse_failures,
                "finalise_attempts": stmt.excluded.finalise_attempts,
            }
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[JourneyFact.run_id, JourneyFact.writer],
                    set_=update_cols,
                )
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "cost_finalize.journey_fact_write_failed",
            extra={"run_id": str(run.id), "writer": writer},
            exc_info=True,
        )


async def _resolve_effective_refs(
    session: AsyncSession,
    run: Run,
    merged_outputs: dict[str, Any],
) -> _JourneyResolution:
    """Self-report confirm + effective-ref merge — the journey resolution.

    Parses the self-report refs from ``merged_outputs``, normalises the reported
    claims, confirms them against existing journey rows (ADVISORY — a reported
    claim can only MATCH, never mint), and merges the create-stamped refs with
    the confirmed reported refs (dedup, cap 100).
    """
    raw = parse_self_report_refs(merged_outputs)
    reported, counters = validate_and_normalise_reported_refs(raw)
    confirmed = await _confirm_reported_refs(session, run.organisation_id, reported)
    effective = _merge_effective_refs(run.work_item_refs, confirmed)
    return _JourneyResolution(
        raw=raw,
        reported=reported,
        confirmed=confirmed,
        counters=counters,
        effective=effective,
    )


async def _advance_journeys_on_terminal(
    session: AsyncSession,
    run: Run,
    status: str,
    merged_outputs: dict[str, Any],
    writer: str = _WRITER_LIVE,
) -> None:
    """FAR-143 finalise hook — self-report confirm + journey advancement.

    FAIL-OPEN and isolated in its own SAVEPOINT (``begin_nested``), separate
    from the ``record_run_facts`` savepoint: a journey-write failure must NEVER
    affect the terminal status write or the run finalisation (the
    cost/ledger/analytics outcome). The steps (parse -> confirm -> advance)
    share the savepoint, so a throw rolls back only the journey work and the
    hook logs + swallows.

    ``status`` is the run's terminal status (``complete`` / ``failed`` /
    ``eval_failed`` / ``cancelled``); ``advance_journeys`` internally decides
    advancing vs mint-only from it. ``merged_outputs`` is the merged output set
    the finalize path is about to persist (or already persisted) — self-report
    refs are parsed from it. ``writer`` is the finalize write path that drove
    this hook (``live`` / ``fallback`` / ``early_return``) — it labels the
    per-writer parse-failure counters and the persisted
    ``modulo_journey_facts`` denominator. The run row is refreshed first so the
    advance sees the just-written terminal ``status`` / ``completed_at`` even
    when the status write went through the raw fenced UPDATE (which bypasses
    the ORM identity map).

    Besides advancing, the hook records the FAR-143 observability counters and
    the persisted denominators: the self-report entries attempted
    (``finalise_attempts`` = ``len(raw)``), the malformed ones
    (``parse_failures`` = ``counters["malformed"]``), the cap drops
    (``counters["capped"]``) and the advisory refs that matched no existing
    journey (``len(reported) - len(confirmed)``).
    """
    try:
        async with session.begin_nested():
            await session.refresh(run)
            resolution = await _resolve_effective_refs(session, run, merged_outputs)
            if resolution.confirmed:
                run.work_item_refs = resolution.effective
                await session.flush()
            advanced = await advance_journeys(
                session,
                run.organisation_id,
                run_id=run.id,
                pipeline_id=run.pipeline_id,
                refs=resolution.effective,
                status=status,
                completed_at=run.completed_at,
                run_created_at=run.created_at,
                is_replay=bool(run.is_replay),
                variant_group_id=run.variant_group_id,
            )
            await _record_journey_outcome(
                session,
                run,
                writer,
                advanced,
                resolution,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_finalize.journey_advance_failed", extra={"run_id": str(run.id)})


async def _record_journey_outcome(
    session: AsyncSession,
    run: Run,
    writer: str,
    advanced: int,
    resolution: _JourneyResolution,
) -> None:
    """Record the FAR-143 observability counters + persisted fact denominators.

    Runs inside the outer journey savepoint, so a throw rolls back only the
    journey work and is swallowed by the caller.
    """
    record_journey_advance(advanced)
    unmatched = max(len(resolution.reported) - len(resolution.confirmed), 0)
    if resolution.counters["malformed"]:
        record_journey_parse_failure(writer, resolution.counters["malformed"])
    if resolution.counters["capped"]:
        record_self_report_refs_capped(resolution.counters["capped"])
    if unmatched:
        record_unmatched_self_report_refs(unmatched)
    record_journey_finalise_attempt(writer, len(resolution.raw))
    await _record_journey_fact(session, run, writer, resolution.counters["malformed"], len(resolution.raw))
    _log.info(
        "cost_finalize.journey_advanced",
        extra={
            "run_id": str(run.id),
            "writer": writer,
            "parsed": resolution.counters["valid"],
            "confirmed": len(resolution.confirmed),
            "malformed": resolution.counters["malformed"],
            "capped": resolution.counters["capped"],
        },
    )


def _log_union_size_guardrail(enriched: dict[str, dict[str, Any]], run_id: uuid.UUID) -> None:
    """Log-only guardrail on the enriched-union JSON size (§4.2)."""
    size_bytes = len(str(enriched).encode("utf-8"))
    if size_bytes > _UNION_SIZE_GUARDRAIL_BYTES:
        _log.warning(
            "cost_union.size_guardrail",
            extra={"run_id": str(run_id), "size_bytes": size_bytes},
        )


async def finalize_cost(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    status: str,
    segment_node_token_usage: dict[str, Any] | None,
    segment_completed_node_outputs: dict[str, Any] | None,
    node_type_map: dict[str, str],
    error_code: str | None = None,
    error_detail: str | None = None,
    is_terminal: bool = True,
    session_factory: Callable[[], Any] | None = None,
    claim_token: str | None = None,
) -> None:
    """The SINGLE finalization block (§4.2) — component read + build + run write + ledger.

    Runs INSIDE the caller's existing ``session.begin()`` (the ACTIVE
    TRANSACTION CONTRACT): ``finalize_cost`` never opens its own nested
    ``begin()`` on an ``autobegin=False`` session; the ONLY nesting is the
    ledger block's ``begin_nested()`` savepoints. ``set_rls_org`` must have
    been called by the caller.

    ``session_factory`` is used ONLY by the reduced escape (a FRESH
    transaction); the executor passes its ``async_sessionmaker``.

    *claim_token* (dist/runtime-core A1) fences the terminal/pause status write:
    a superseded executor's token no longer matches and the write is a no-op
    (logged, skipped) so it cannot terminalize the run out from under a
    successor. CANCEL-WINS (B6): finalizing an ``awaiting_human``/``complete``
    run whose row carries ``cancellation_requested`` writes ``cancelled``
    instead (the same statement is guard-atomic for the concurrent case).
    """
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        _log.warning("cost_finalize.run_not_found", extra={"run_id": str(run_id)})
        return

    status = _apply_cancel_wins(run, status)

    merged_usage = _merge(run.node_token_usage, segment_node_token_usage, segment_wins=True)
    merged_outputs, merged_telemetry = _split_merge_outputs(
        run.outputs_json,
        run.node_telemetry_json,
        segment_completed_node_outputs,
        node_type_map,
        run_id=str(run.id),
    )

    if _is_empty_finalize_segment(merged_usage, merged_outputs, merged_telemetry):
        # Pre-component-read terminal: total 0, breakdown NULL, no ledger.
        await _write_empty_terminal(
            session,
            run_id,
            _TerminalWrite(status, error_code, error_detail, claim_token),
            is_terminal,
            run,
            merged_outputs,
        )
        return

    # --- the never-fail envelope: component read + build + run write (§1.5) ---
    try:
        built = await _build_enriched_state(
            session,
            run,
            merged_usage,
            merged_outputs,
            merged_telemetry,
            node_type_map,
            is_terminal,
        )
        # FAR-104 — per-agent token budget enforcement (TERMINAL-ONLY, atomic).
        # Runs AFTER the run's token usage is derived (SERVER-measured entries)
        # and BEFORE the status write, so the ``budget_exceeded`` status +
        # error message land in the SAME ``update_run_status`` call. Cancelled
        # (CANCEL-WINS) and eval_failed (the eval gate outcome) are preserved.
        status, error_code, error_detail = await _apply_agent_budget_override(
            session, run, built.enriched, is_terminal, status, error_code, error_detail
        )
        await _write_finalized_run(
            session,
            run_id,
            merged_outputs,
            merged_telemetry,
            built,
            _TerminalWrite(status, error_code, error_detail, claim_token),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_component_finalize_failed", extra={"run_id": str(run_id)})
        record_fallback_legacy()
        # FAR-104 — the budget check is FAIL-OPEN (never raises), so it is safe
        # inside the never-fail fallback envelope: an agent-budget breach still
        # terminalizes ``budget_exceeded`` even when the component build failed.
        status, error_code, error_detail = await _apply_agent_budget_override(
            session, run, merged_usage, is_terminal, status, error_code, error_detail
        )
        await _fallback_write(
            session,
            run_id,
            status,
            _MergedSets(merged_usage, merged_outputs, merged_telemetry),
            error_code,
            error_detail,
            is_terminal=is_terminal,
            claim_token=claim_token,
        )
        return

    # --- Ledger block — terminal only, guarded, converged (§4.2/§4.6) ---
    run_date = _ledger_run_date(is_terminal, built.total, run)
    if run_date is not None:
        await _ledger_block(
            session,
            run_id=run_id,
            org_id=org_id,
            status=status,
            total=built.total,
            owner_team_id=run.owner_team_id,
            run_date=run_date,
            finalize_fields={
                "error_code": error_code,
                "error_detail": error_detail,
                "total_cost_usd": built.total,
                "cost_breakdown": built.breakdown,
                "node_token_usage": built.enriched,
                "outputs_json": merged_outputs,
                "node_telemetry_json": merged_telemetry,
                "total_tokens": built.total_tokens,
            },
            session_factory=session_factory,
            claim_token=claim_token,
        )

    # --- Analytics facts — every terminal path, SAME transaction (ADR 020) ---
    # ``record_run_facts`` is fail-open: a facts-write failure rolls back only
    # its own savepoint and never affects the cost/ledger outcome. The run is
    # refreshed INSIDE ``record_run_facts``'s guard (the fenced UPDATE bypasses
    # the ORM identity map, so the fact must snapshot the terminal row — FAR-200);
    # a refresh failure degrades to the in-memory object and never propagates.
    if is_terminal:
        await _record_terminal_analytics(session, run, status, merged_outputs, writer=_WRITER_LIVE)


def _apply_cancel_wins(run: Run, status: str) -> str:
    """B6 — CANCEL-WINS precedence: an interrupted/awaiting_human (or about-to-be
    completed) run with a cancellation requested is finalised ``cancelled``,
    never ``awaiting_human``/``complete``."""
    if getattr(run, "cancellation_requested", False) and status in ("awaiting_human", "complete"):
        _log.info("cost_finalize.cancel_wins", extra={"run_id": str(run.id), "status": status})
        return "cancelled"
    return status


def _is_empty_finalize_segment(
    merged_usage: dict[str, Any],
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
) -> bool:
    """True when NO node contributed usage/outputs/telemetry — the pre-component-read
    terminal (total 0, breakdown NULL, no ledger)."""
    return not merged_usage and not merged_outputs and not merged_telemetry


def _ledger_run_date(is_terminal: bool, total: Decimal, run: Run) -> date | None:
    """The ledger run-date when a terminal run should enter the ledger.

    Terminal-only gate: a positive persisted total with a started timestamp
    yields the UTC run date; otherwise ``None`` (no ledger entry).
    """
    if is_terminal and total is not None and total > 0 and run.started_at is not None:
        return run.started_at.astimezone(UTC).date()
    return None


async def _build_enriched_state(
    session: AsyncSession,
    run: Run,
    merged_usage: dict[str, Any],
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    node_type_map: dict[str, str],
    is_terminal: bool,
) -> _BuiltCost:
    """The cost build — component read + enrich + telemetry + breakdown + write-back.

    Runs inside the never-fail envelope. Returns the persisted
    ``total``/``breakdown``, the ENRICHED union (``node_token_usage``) and the
    derived server-measured token total. Any exception here is caught by the
    caller's LEGACY FALLBACK.
    """
    live_components = await load_live_components(session, run.organisation_id)
    enriched = _enrich_union(
        merged_usage,
        merged_outputs,
        node_type_map,
        is_terminal=is_terminal,
        merged_telemetry=merged_telemetry,
    )
    telemetry, per_node_cost = build_telemetry(enriched, live_components)
    breakdown, total = build_cost_breakdown(telemetry, live_components, settings=get_settings())
    enriched = _write_back_node_cost(enriched, per_node_cost)
    total_tokens = _derive_total_tokens(enriched)
    _log_union_size_guardrail(enriched, run.id)
    return _BuiltCost(total=total, breakdown=breakdown, enriched=enriched, total_tokens=total_tokens)


async def _write_finalized_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
    built: _BuiltCost,
    write: _TerminalWrite,
) -> None:
    """Persist the enriched finalization — the single ``update_run_status`` write."""
    await update_run_status(
        session,
        run_id,
        write.status,
        error_code=write.error_code,
        error_detail=write.error_detail,
        total_cost_usd=built.total,
        cost_breakdown=built.breakdown,
        node_token_usage=built.enriched,
        outputs_json=merged_outputs,
        node_telemetry_json=merged_telemetry,
        total_tokens=built.total_tokens,
        claim_token=write.claim_token,
    )


async def _record_terminal_analytics(
    session: AsyncSession,
    run: Run,
    status: str,
    merged_outputs: dict[str, Any],
    writer: str,
) -> None:
    """Terminal analytics + journey advancement in the SAME transaction (ADR 020).

    ``record_run_facts`` is fail-open: a facts-write failure rolls back only
    its own savepoint and never affects the cost/ledger outcome. The run is
    refreshed INSIDE ``record_run_facts``'s guard (the fenced UPDATE bypasses
    the ORM identity map, so the fact must snapshot the terminal row — FAR-200);
    a refresh failure degrades to the in-memory object and never propagates.
    ``_advance_journeys_on_terminal`` is the FAR-143 self-report confirm +
    journey advancement hook (fail-open, own savepoint). Also covers the
    reduced-escape terminal (its fresh-tx status write is committed before we
    get here).
    """
    await record_run_facts(session, run)
    await _advance_journeys_on_terminal(session, run, status, merged_outputs, writer=writer)


async def _apply_agent_budget_override(
    session: AsyncSession,
    run: Run,
    usage: dict[str, Any],
    is_terminal: bool,
    status: str,
    error_code: str | None,
    error_detail: str | None,
) -> tuple[str, str | None, str | None]:
    """FAR-104 — per-agent token budget enforcement (TERMINAL-ONLY, atomic, fail-open).

    Returns the (possibly overridden) ``status`` / ``error_code`` /
    ``error_detail`` triple. Cancelled (CANCEL-WINS) and eval_failed (the eval
    gate outcome) are preserved.
    """
    if is_terminal and status not in ("cancelled", "eval_failed"):
        budget_override = await _enforce_agent_token_budgets(session, run=run, usage=usage)
        if budget_override is not None:
            return budget_override
    return status, error_code, error_detail


async def _write_empty_terminal(
    session: AsyncSession,
    run_id: uuid.UUID,
    write: _TerminalWrite,
    is_terminal: bool,
    run: Run,
    merged_outputs: dict[str, Any],
) -> None:
    """The PRE-COMPONENT-READ terminal write — total 0, breakdown NULL, no ledger."""
    await update_run_status(
        session,
        run_id,
        write.status,
        error_code=write.error_code,
        error_detail=write.error_detail,
        total_cost_usd=Decimal(0),
        total_tokens=0,
        claim_token=write.claim_token,
    )
    if is_terminal:
        # Refresh AFTER the status write — the fenced UPDATE bypasses the
        # ORM identity map, so without it the fact would snapshot the
        # pre-write 'running' row (FAR-200). The refresh runs INSIDE
        # ``record_run_facts``'s fail-open guard (ADR-020), so a refresh
        # failure degrades to the in-memory object and never propagates.
        # FAR-143 — even with empty outputs the run still advances from its
        # create-stamped refs (zero-cost terminal).
        await _record_terminal_analytics(session, run, write.status, merged_outputs, writer=_WRITER_EARLY_RETURN)


async def _load_node_type_map(session: AsyncSession, snapshot_id: uuid.UUID) -> dict[str, str]:
    """Derive the run-frozen node-type map from the snapshot's ``graph_json``.

    Edge-synthesized HITL gate nodes are absent from ``graph_json.nodes`` but
    encoded on the edges, so the derived map is EXTENDED from the edges —
    gate envelopes resolve by type in the split-then-merge (FAR-125 P1b).
    """
    result = await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))
    graph_json = result.scalar_one_or_none()
    return extend_node_type_map_from_edges(derive_node_type_map(graph_json), graph_json)


async def _load_node_agent_map(session: AsyncSession, snapshot_id: uuid.UUID) -> dict[str, str]:
    """Derive the run-frozen node→agent map from the snapshot's ``graph_json``.

    FAR-104 per-agent token budget enforcement reads the run's snapshot graph
    (immutable per snapshot) to attribute each node's SERVER-measured token
    usage to its agent. Edge-synthesized HITL gate nodes carry no ``agent_id``
    and are absent from the map.
    """
    result = await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))
    return derive_node_agent_map(result.scalar_one_or_none())


async def _enforce_agent_token_budgets(
    session: AsyncSession,
    *,
    run: Run,
    usage: dict[str, Any] | None,
) -> tuple[str, str, str] | None:
    """FAR-104: per-agent token budget enforcement at finalization.

    Returns an ``(status, error_code, error_detail)`` override tuple when ANY
    agent referenced by the run's snapshot graph has accumulated more tokens
    across its nodes than its ``token_budget``, else ``None``.

    The check runs in the TERMINAL path AFTER the run's token usage has been
    derived (from the enriched union / merged usage — the SERVER-measured
    entries, the same authority ``_derive_total_tokens`` uses). The caller
    writes the returned terminal override (``budget_exceeded`` + the PRD error
    message) atomically in the SAME ``update_run_status`` call that persists
    the finalization — never a second write.

    FAIL-OPEN with a log: a budget-check DB failure (agent lookup / snapshot
    read) returns ``None`` and does NOT fail the terminal path — finalization
    must survive (the never-fail envelope, §1.5).
    """
    try:
        node_agent_map = await _load_node_agent_map(session, run.snapshot_id)
        agent_ids = {agent_id for agent_id in node_agent_map.values() if agent_id}
        if not agent_ids:
            return None
        agent_result = await session.execute(select(Agent.id, Agent.token_budget).where(Agent.id.in_(agent_ids)))
        budgets: dict[str, int] = {}
        for agent_id, token_budget in agent_result.all():
            if token_budget is not None:
                budgets[str(agent_id)] = int(token_budget)
        if not budgets:
            return None
        per_agent = _accumulate_agent_tokens(usage, node_agent_map)
        for agent_id, budget in budgets.items():
            if per_agent.get(agent_id, 0) > budget:
                _log.warning(
                    "cost_ledger.token_budget_exceeded",
                    extra={
                        "run_id": str(run.id),
                        "agent_id": agent_id,
                        "accumulated_tokens": per_agent.get(agent_id, 0),
                        "token_budget": budget,
                    },
                )
                return _BUDGET_EXCEEDED_STATUS, _BUDGET_EXCEEDED_ERROR_CODE, _BUDGET_EXCEEDED_MESSAGE
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "cost_finalize.token_budget_check_failed",
            extra={"run_id": str(run.id)},
            exc_info=True,
        )
        return None


async def finalize_cancelled_run(session: AsyncSession, *, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Route a ``request_cancellation`` terminal write through ``finalize_cost``.

    The cancel path runs in a SEPARATE process from the executor, so the
    in-memory accumulated sets are NOT available. It RE-READS the STORED
    cumulative sets (``run.outputs_json`` + ``run.node_token_usage`` +
    ``run.node_telemetry_json``) and passes THOSE to ``finalize_cost`` (§4.2
    DATA SOURCE PINNED). A streamed run that HAS PAUSED at least once has
    stored sets → a partial breakdown + ONE ledger row. A NEVER-PAUSED in-flight
    run has NO stored sets → its accrued cost is FORFEITED and only the
    ``cost_components_partial_spend_lost`` diagnostic log fires (run_id only —
    the cancel process lacks the in-memory dicts, so the accrued segment count
    is never determinable).

    Both stored output columns are re-fed (FAR-125 P1b): ``outputs_json`` as
    the segment and ``node_telemetry_json`` as the split signal read inside
    ``finalize_cost``, so already-pure rows are idempotent no-ops and legacy
    rows are split exactly once.
    """
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        return
    if not (run.outputs_json or run.node_token_usage or run.node_telemetry_json):
        _log.warning("cost_components_partial_spend_lost", extra={"run_id": str(run_id)})
        return
    node_type_map = await _load_node_type_map(session, run.snapshot_id)
    await finalize_cost(
        session,
        run_id=run_id,
        org_id=org_id,
        status="cancelled",
        segment_node_token_usage=run.node_token_usage,
        segment_completed_node_outputs=run.outputs_json,
        node_type_map=node_type_map,
        is_terminal=True,
        session_factory=None,
    )
