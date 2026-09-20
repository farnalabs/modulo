"""FAR-902: Schema enforcement observability — per-attempt telemetry + aggregation.

This module provides:

- ``SchemaEnforcementRecord`` dataclass: the per-attempt enforcement payload
  written to ``run_node_outputs.schema_enforcement_json`` BEFORE terminalization
  (D3 ordering guarantee).
- ``build_enforcement_record``: pure function that constructs the record from
  the validation outcome, profile, and repair loop state.
- ``aggregate_run_enforcement``: pure function that aggregates per-attempt
  records into run-level counters (native/verbatim/repair/wasted).
- ``MAX_ENFORCEMENT_PAYLOAD_BYTES``: the 64 KB hard cap on the serialised
  enforcement payload.

The enforcement record is a PURE telemetry payload — it does not gate recovery
(``recover_node`` must NOT consult it) and never writes into the Agent Return
Contract columns (``outputs_json`` / ``node_telemetry_json``).

The flip guard (D5) lives here as a separate concern — it advises on the
lenient-to-strict transition based on accumulated lenient-mode warnings.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from modulo.core.pipeline_engine.schema_repair import SchemaValidationOutcome

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard cap on the serialised enforcement payload (64 KB).
# ---------------------------------------------------------------------------

MAX_ENFORCEMENT_PAYLOAD_BYTES = 65_536

# Maximum validation errors to retain in the payload (bound + truncation flag).
_MAX_VALIDATION_ERRORS = 50


# ---------------------------------------------------------------------------
# Per-attempt enforcement record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaEnforcementRecord:
    """Per-attempt schema enforcement telemetry — the FAR-902 payload.

    Written to ``run_node_outputs.schema_enforcement_json`` for every
    non-``__final__`` attempt that has a schema assigned.  Bound to 64 KB:
    the first N validation errors are kept, with an explicit count and
    truncation flag.

    Fields:

    - ``outcome``: the ``SchemaValidationOutcome`` value for this attempt.
    - ``resolved_profile``: the schema profile used (verbatim, provider-strict,
      runtime-sdk, or None when no schema was assigned).
    - ``native_output``: True when the sandbox returned structured output via
      the native JSON channel (``supports_native_structured_output``).
    - ``repair_attempts``: count of repair loop invocations on this attempt.
    - ``wasted_attempts``: count of attempts whose sole failure cause was
      schema rejection (the repair loop consumed budget but produced nothing
      usable).
    - ``validation_errors``: the first N structured validation error dicts,
      with ``total_count`` and ``truncated`` flags when the list exceeds the
      cap.
    """

    outcome: str
    resolved_profile: str | None = None
    native_output: bool = False
    repair_attempts: int = 0
    wasted_attempts: int = 0
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    total_error_count: int = 0
    truncated: bool = False


def build_enforcement_record(
    *,
    outcome: str,
    resolved_profile: str | None = None,
    native_output: bool = False,
    repair_attempts: int = 0,
    wasted_attempts: int = 0,
    validation_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a schema enforcement record payload.

    Returns a JSON-serialisable dict bound to 64 KB, or ``None`` when the
    outcome is ``NO_SCHEMA`` (no enforcement data to record).

    The validation error list is truncated to ``_MAX_VALIDATION_ERRORS`` with
    ``total_count`` and ``truncated`` flags when the full list exceeds the cap.
    The 64 KB bound is enforced on the serialised form; if the truncated
    payload still exceeds the cap, further errors are dropped until it fits
    or a single-error payload is still too large (logged as a warning).
    """
    if outcome == SchemaValidationOutcome.NO_SCHEMA.value:
        return None

    errors = validation_errors or []
    total_count = len(errors)
    truncated = total_count > _MAX_VALIDATION_ERRORS
    kept_errors = errors[:_MAX_VALIDATION_ERRORS]

    record = SchemaEnforcementRecord(
        outcome=outcome,
        resolved_profile=resolved_profile,
        native_output=native_output,
        repair_attempts=repair_attempts,
        wasted_attempts=wasted_attempts,
        validation_errors=kept_errors,
        total_error_count=total_count,
        truncated=truncated,
    )

    payload = asdict(record)

    # Enforce 64 KB hard cap — progressively drop errors until it fits.
    serialised = json.dumps(payload, default=str, ensure_ascii=True)
    if len(serialised.encode("utf-8")) > MAX_ENFORCEMENT_PAYLOAD_BYTES:
        # Drop errors in chunks until it fits.
        while len(payload["validation_errors"]) > 0:
            payload["validation_errors"] = payload["validation_errors"][:-5]
            payload["total_error_count"] = total_count
            payload["truncated"] = True
            serialised = json.dumps(payload, default=str, ensure_ascii=True)
            if len(serialised.encode("utf-8")) <= MAX_ENFORCEMENT_PAYLOAD_BYTES:
                break
        else:
            # Even with zero errors, the payload is too large — log and return
            # what we have (best-effort, fail-open).
            _log.warning(
                "schema_enforcement.payload_exceeds_cap",
                extra={"size": len(serialised.encode("utf-8"))},
            )

    return payload


# ---------------------------------------------------------------------------
# Run-level aggregation (pure function)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunEnforcementAggregates:
    """Run-level aggregate counters derived from per-attempt enforcement records.

    Written to ``run_daily_facts`` columns (D4) by the aggregation step.
    """

    native_count: int = 0
    verbatim_count: int = 0
    repair_count: int = 0
    wasted_count: int = 0
    total_attempts: int = 0
    enforcement_record_count: int = 0


# Outcomes classified as "native" — the sandbox returned structured output.
_NATIVE_OUTCOMES = frozenset(
    {
        SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value,
        SchemaValidationOutcome.NATIVE_DECODE_FAILED.value,
        SchemaValidationOutcome.NATIVE_DECODE_NOT_JSON.value,
    }
)

# Outcomes classified as "verbatim" — the sandbox returned raw text.
_VERBATIM_OUTCOMES = frozenset(
    {
        SchemaValidationOutcome.VERBATIM_PASSED.value,
        SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value,
        SchemaValidationOutcome.LENIENT_VALIDATION_BYPASSED.value,
        SchemaValidationOutcome.REPAIR_EXHAUSTED.value,
        SchemaValidationOutcome.REPAIR_ATTEMPTED.value,
        SchemaValidationOutcome.PASSED_AFTER_REPAIR.value,
        SchemaValidationOutcome.SCHEMA_UNENFORCEABLE.value,
    }
)


def aggregate_run_enforcement(
    enforcement_records: list[dict[str, Any]],
) -> RunEnforcementAggregates:
    """Aggregate per-attempt enforcement records into run-level counters.

    Pure function — no DB, no I/O.  Accepts a list of serialised
    ``SchemaEnforcementRecord`` dicts (from ``run_node_outputs`` rows) and
    returns the aggregate counters for writing to ``run_daily_facts``.

    ``wasted_count`` sums the ``wasted_attempts`` field across all records
    (each record's wasted count is the number of attempts on that node
    whose sole failure was schema rejection).
    """
    native = 0
    verbatim = 0
    repair = 0
    wasted = 0
    total = len(enforcement_records)

    for record in enforcement_records:
        outcome = record.get("outcome", "")
        if outcome in _NATIVE_OUTCOMES:
            native += 1
        elif outcome in _VERBATIM_OUTCOMES:
            verbatim += 1
        repair += record.get("repair_attempts", 0)
        wasted += record.get("wasted_attempts", 0)

    return RunEnforcementAggregates(
        native_count=native,
        verbatim_count=verbatim,
        repair_count=repair,
        wasted_count=wasted,
        total_attempts=total,
        enforcement_record_count=total,
    )
