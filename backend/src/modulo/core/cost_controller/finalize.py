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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.analytics import record_run_facts
from modulo.core.cost_controller import check_and_record_spend
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
from modulo.core.node_output_split import (
    extend_node_type_map_from_edges,
    node_telemetry,
    split_node_output,
)
from modulo.db.crud.run import update_run_status
from modulo.db.models.cost_component import CostComponent
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

__all__ = [
    "derive_node_type_map",
    "finalize_cancelled_run",
    "finalize_cost",
    "load_live_components",
]

# Union JSON size guardrail — log-only, not a cap (§4.2).
_UNION_SIZE_GUARDRAIL_BYTES = 8 * 1024 * 1024

_LEGACY_E2B_RATE_DEFAULT = Decimal("0.13")


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

    for node_id, value in stored_out.items():
        node_id = str(node_id)
        if node_id in stored_tel:
            merged_outputs[node_id] = value
            merged_telemetry[node_id] = stored_tel[node_id]
            continue
        _log_output_resplit(run_id, node_id)
        ret, telemetry = split_node_output(value, node_type_map.get(node_id, ""), None, run_id=run_id, node_id=node_id)
        if not (ret is None and telemetry.get("skipped") is True):
            merged_outputs[node_id] = ret
        merged_telemetry[node_id] = telemetry

    if isinstance(segment, dict):
        for node_id, seg_value in segment.items():
            node_id = str(node_id)
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
            if not (ret is None and telemetry.get("skipped") is True):
                merged_outputs[node_id] = ret
            merged_telemetry[node_id] = telemetry

    return merged_outputs, merged_telemetry


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


def _fold_model_cost(node_dict: dict[str, Any], output_obj: dict[str, Any] | None) -> None:
    """The PINNED stored-union ONE-mechanism rule for ``model_cost_usd`` (§4.2/§4.5).

    (1) output PRESENT + carries ``model_cost_usd`` → OVERWRITE with the
        re-clamped fold (re-validated through ``clamp_reported`` — the FULL
        mirror of the extraction validation, defense-in-depth; the input is the
        RAW field when present, else the clamped value — the explicit-None pin);
    (2) output PRESENT but LACKS ``model_cost_usd`` → pop the value + sibling
        flags (the node is estimated);
    (3) output ABSENT from both stored ``outputs_json`` and the current segment
        → the stored-union value is re-clamped through ``clamp_reported`` and
        the folded flags derive from the re-clamped fold (fallback authority —
        the third-path class).

    ``model_cost_clamped`` / ``model_cost_out_of_band_high`` are the
    AUTHORITATIVE values folded from the node-output dict written by
    extraction; ``clamp_reported``'s own flags are the fallback only when the
    output lacks them.
    """
    if output_obj is None:
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
        return

    if "model_cost_usd" in output_obj:
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
        has_wallclock = isinstance(output_obj, dict) and isinstance(output_obj.get("wall_clock_time_ms"), (int, float))
        if isinstance(output_obj, dict) and isinstance(output_obj.get("wall_clock_time_ms"), (int, float)):
            node_dict["wall_clock_time_ms"] = output_obj["wall_clock_time_ms"]

        map_type = node_type_map.get(node_id)
        node_dict["sandbox_by_map"] = map_type == NODE_TYPE_SANDBOX_AGENT
        node_dict["is_sandbox_for_wallclock"] = (map_type == NODE_TYPE_SANDBOX_AGENT) or (
            map_type is None and has_wallclock
        )

        _fold_model_cost(node_dict, output_obj)

        if output_obj is not None:
            executed_types[map_type or "<map_absent>"] += 1
            if map_type is None:
                missing_node_type.append(node_id)
            if is_terminal:
                schema_drift = output_obj.get("schema_drift")
                if schema_drift is None and isinstance(output_obj.get("output_json"), dict):
                    schema_drift = output_obj["output_json"].get("schema_drift")
                if schema_drift and output_obj.get("pin_failed") is not True and map_type == NODE_TYPE_SANDBOX_AGENT:
                    record_schema_drift()

    if missing_node_type:
        _log.warning("cost_components_missing_node_type", extra={"node_ids": missing_node_type})
    if executed_types:
        _log.info("cost_components_node_type_ratio", extra={"executed_types": dict(executed_types)})

    return union


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
    merged_usage: dict[str, Any],
    merged_outputs: dict[str, Any],
    merged_telemetry: dict[str, Any],
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
    total_tokens = _derive_total_tokens(merged_usage)
    token_cost = _token_cost(merged_usage)
    sandbox_cost = _legacy_sandbox_cost(merged_outputs, merged_telemetry)
    total = token_cost + sandbox_cost
    if not total.is_finite():
        total = Decimal(0)
    wall_hours = 0.0
    if isinstance(merged_outputs, dict):
        for node_id in merged_outputs:
            out = node_telemetry(merged_telemetry, merged_outputs, node_id)
            if isinstance(out, dict) and isinstance(out.get("wall_clock_time_ms"), (int, float)):
                wall_hours += float(out["wall_clock_time_ms"]) / 3600000.0
    breakdown: list[dict[str, Any]] = [
        {
            "component": "llm_tokens",
            "display_name": "LLM Tokens",
            "source": "calculated",
            "amount_usd": _entry_amount(token_cost),
            "formula_applied": ("tokens_input * input_token_rate + tokens_output * output_token_rate"),
            "rate_usd": None,
            "basis": {
                "tokens_input": int(
                    sum((e.get("input_tokens") or 0) for e in (merged_usage or {}).values() if isinstance(e, dict))
                ),
                "tokens_output": int(
                    sum((e.get("output_tokens") or 0) for e in (merged_usage or {}).values() if isinstance(e, dict))
                ),
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
        node_token_usage=merged_usage,
        outputs_json=merged_outputs,
        node_telemetry_json=merged_telemetry,
        total_tokens=total_tokens,
        claim_token=claim_token,
    )
    if is_terminal:
        run = await session.get(Run, run_id)
        if run is not None:
            await record_run_facts(session, run)


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
    session: AsyncSession,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    status: str,
    finalize_fields: dict[str, Any],
    session_factory: Callable[[], Any] | None,
    claim_token: str | None = None,
) -> None:
    """The REDUCED terminalize-without-ledger escape (§4.2).

    Persists the FULL finalization field set in a FRESH transaction, sets
    NOTHING ELSE, leaves ``ledger_written = false``. Engages ONLY for genuine
    write failures, never a ``daily_limit_exceeded`` refusal. The status write
    is fenced by *claim_token* (a superseded executor's escape is a no-op).
    """
    if session_factory is None:
        _log.error("cost_ledger.reduced_escape_unavailable", extra={"run_id": str(run_id)})
        return
    try:
        async with session_factory() as fresh, fresh.begin():
            await set_rls_org(fresh, org_id)
            run = await update_run_status(
                fresh,
                run_id,
                status,
                **finalize_fields,
                claim_token=claim_token,
            )
            if run is not None:
                await record_run_facts(fresh, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_ledger.reduced_escape_failed", extra={"run_id": str(run_id)})


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

    if not ok and reason is not None and reason.startswith("daily_limit_exceeded"):
        # LIMIT-REFUSED — expected healthy enforcement, NOT a ledger failure.
        locked.ledger_refused_at = datetime.now(UTC)
        record_limit_refused(str(owner_team_id or "none"))
        _log.info("cost_ledger.limit_reached", extra={"run_id": str(run_id)})
        await session.flush()
        return

    if not ok:
        # REDUCED terminalize-without-ledger escape, write_failure ONLY.
        _log.error(
            "cost_ledger.finalize_deferred",
            extra={"reason": reason or "unknown", "run_id": str(run_id)},
        )
        record_finalize_deferred(reason="write_failure", team=str(owner_team_id or "none"))
        await _reduced_escape(
            session,
            run_id,
            org_id,
            status,
            finalize_fields,
            session_factory,
            claim_token=claim_token,
        )
        return

    locked.ledger_written = True
    await session.flush()


async def _record_duplicate_terminal_event(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Record a duplicate-terminal event for the probe's FLOOD trigger (§4.7).

    The event rides in a bounded ``duplicate_terminal_events`` list on the
    GLOBAL ``system_config`` (NO RLS — the same discipline as ``probe_state``),
    under the advisory-lock read-modify-write. The probe counts DISTINCT
    run-ids within the 10-minute window; a stale event log is harmless (the
    probe trims on read). Never raises — a duplicate guard firing must not fail
    the terminal path.
    """
    from datetime import UTC, datetime, timedelta

    from modulo.core.cost_controller.system_config import (
        acquire_kv_lock,
        read_system_config,
        write_system_config,
    )

    key = "duplicate_terminal_events"
    try:
        await acquire_kv_lock(session, key)
        events = await read_system_config(session, key) or []
        if not isinstance(events, list):
            events = []
        events.append({"run_id": str(run_id), "ts": datetime.now(UTC).isoformat()})
        cutoff = datetime.now(UTC) - timedelta(seconds=10 * 60)
        kept = []
        for event in events[-200:]:
            ts = event.get("ts")
            try:
                from datetime import datetime as _dt

                parsed = _dt.fromisoformat(ts) if ts else None
            except (ValueError, TypeError):
                parsed = None
            if parsed is None or parsed >= cutoff:
                kept.append(event)
        await write_system_config(session, key, kept[-100:])
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_ledger.duplicate_event_record_failed", extra={"run_id": str(run_id)})


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

    # B6 — CANCEL-WINS precedence: an interrupted/awaiting_human (or about-to-be
    # completed) run with a cancellation requested is finalised ``cancelled``,
    # never ``awaiting_human``/``complete``.
    if getattr(run, "cancellation_requested", False) and status in ("awaiting_human", "complete"):
        _log.info("cost_finalize.cancel_wins", extra={"run_id": str(run_id), "status": status})
        status = "cancelled"

    merged_usage = _merge(run.node_token_usage, segment_node_token_usage, segment_wins=True)
    merged_outputs, merged_telemetry = _split_merge_outputs(
        run.outputs_json,
        run.node_telemetry_json,
        segment_completed_node_outputs,
        node_type_map,
        run_id=str(run.id),
    )

    if not merged_usage and not merged_outputs and not merged_telemetry:
        # Pre-component-read terminal: total 0, breakdown NULL, no ledger.
        await update_run_status(
            session,
            run_id,
            status,
            error_code=error_code,
            error_detail=error_detail,
            total_cost_usd=Decimal(0),
            total_tokens=0,
            claim_token=claim_token,
        )
        if is_terminal:
            await record_run_facts(session, run)
        return

    # --- the never-fail envelope: component read + build + run write (§1.5) ---
    try:
        live_components = await load_live_components(session, run.organisation_id)
        enriched = _enrich_union(
            merged_usage,
            merged_outputs,
            node_type_map,
            is_terminal=is_terminal,
            merged_telemetry=merged_telemetry,
        )
        from modulo.settings import get_settings

        telemetry, per_node_cost = build_telemetry(enriched, live_components)
        breakdown, total = build_cost_breakdown(telemetry, live_components, settings=get_settings())
        enriched = _write_back_node_cost(enriched, per_node_cost)
        total_tokens = _derive_total_tokens(enriched)
        if len(str(enriched).encode("utf-8")) > _UNION_SIZE_GUARDRAIL_BYTES:
            _log.warning(
                "cost_union.size_guardrail",
                extra={"run_id": str(run_id), "size_bytes": len(str(enriched).encode("utf-8"))},
            )
        await update_run_status(
            session,
            run_id,
            status,
            error_code=error_code,
            error_detail=error_detail,
            total_cost_usd=total,
            cost_breakdown=breakdown,
            node_token_usage=enriched,
            outputs_json=merged_outputs,
            node_telemetry_json=merged_telemetry,
            total_tokens=total_tokens,
            claim_token=claim_token,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_component_finalize_failed", extra={"run_id": str(run_id)})
        record_fallback_legacy()
        await _fallback_write(
            session,
            run_id,
            status,
            merged_usage,
            merged_outputs,
            merged_telemetry,
            error_code,
            error_detail,
            is_terminal=is_terminal,
            claim_token=claim_token,
        )
        return

    # --- Ledger block — terminal only, guarded, converged (§4.2/§4.6) ---
    if is_terminal and total is not None and total > 0 and run.started_at is not None:
        await _ledger_block(
            session,
            run_id=run_id,
            org_id=org_id,
            status=status,
            total=total,
            owner_team_id=run.owner_team_id,
            run_date=run.started_at.astimezone(UTC).date(),
            finalize_fields={
                "error_code": error_code,
                "error_detail": error_detail,
                "total_cost_usd": total,
                "cost_breakdown": breakdown,
                "node_token_usage": enriched,
                "outputs_json": merged_outputs,
                "node_telemetry_json": merged_telemetry,
                "total_tokens": total_tokens,
            },
            session_factory=session_factory,
            claim_token=claim_token,
        )

    # --- Analytics facts — every terminal path, SAME transaction (ADR 020) ---
    # ``record_run_facts`` is fail-open: a facts-write failure rolls back only
    # its own savepoint and never affects the cost/ledger outcome.
    if is_terminal:
        await record_run_facts(session, run)


async def _load_node_type_map(session: AsyncSession, snapshot_id: uuid.UUID) -> dict[str, str]:
    """Derive the run-frozen node-type map from the snapshot's ``graph_json``.

    Edge-synthesized HITL gate nodes are absent from ``graph_json.nodes`` but
    encoded on the edges, so the derived map is EXTENDED from the edges —
    gate envelopes resolve by type in the split-then-merge (FAR-125 P1b).
    """
    result = await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))
    graph_json = result.scalar_one_or_none()
    return extend_node_type_map_from_edges(derive_node_type_map(graph_json), graph_json)


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
