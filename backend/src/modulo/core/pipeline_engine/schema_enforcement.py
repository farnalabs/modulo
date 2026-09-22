"""FAR-902: Schema enforcement observability — per-attempt telemetry + aggregation.

This module provides:

- ``SchemaEnforcementRecord`` dataclass: the per-attempt enforcement payload
  written to ``run_node_outputs.schema_enforcement_json`` BEFORE terminalization
  (D3 ordering guarantee).
- ``build_enforcement_record``: pure function that constructs the record from
  the validation outcome, profile, and repair loop state.
- ``aggregate_run_enforcement``: pure function that aggregates per-attempt
  records into run-level counters (native/verbatim/repair/wasted).
- ``derive_mode_from_records``: pure function that derives the schema validator
  mode from enforcement records (lenient if any lenient bypass exists).
- ``derive_outcome_from_records``: pure function that derives the run-level
  validation outcome from per-attempt records.
- ``FlipGuardVerdict`` + ``compute_flip_guard_verdict``: the lenient-to-strict
  flip guard — advises whether flipping is safe based on accumulated
  lenient-mode warnings.  NEVER mutates the mode.
- ``MAX_ENFORCEMENT_PAYLOAD_BYTES``: the 64 KB hard cap on the serialised
  enforcement payload.

The enforcement record is a PURE telemetry payload — it does not gate recovery
(``recover_node`` must NOT consult it) and never writes into the Agent Return
Contract columns (``outputs_json`` / ``node_telemetry_json``).

The flip guard lives here as a separate concern — it advises on the
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


# ---------------------------------------------------------------------------
# Mode / outcome derivation from enforcement records (FAR-902)
# ---------------------------------------------------------------------------

# Outcomes that indicate lenient mode was active (a bypass = warning only).
_LENIENT_OUTCOMES = frozenset(
    {
        SchemaValidationOutcome.LENIENT_VALIDATION_BYPASSED.value,
    }
)

# Outcomes that would become hard failures in strict mode — these are the
# "warnings" that the flip guard counts.
_STRICT_FAILURE_OUTCOMES = frozenset(
    {
        SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value,
        SchemaValidationOutcome.REPAIR_EXHAUSTED.value,
    }
)

# Outcome severity ranking (lower = more severe).  The run-level outcome is
# the MOST severe outcome observed across all attempts.
_OUTCOME_SEVERITY: dict[str, int] = {
    SchemaValidationOutcome.REPAIR_EXHAUSTED.value: 0,
    SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value: 1,
    SchemaValidationOutcome.NATIVE_DECODE_FAILED.value: 2,
    SchemaValidationOutcome.NATIVE_DECODE_NOT_JSON.value: 3,
    SchemaValidationOutcome.LENIENT_VALIDATION_BYPASSED.value: 4,
    SchemaValidationOutcome.REPAIR_ATTEMPTED.value: 5,
    SchemaValidationOutcome.VERBATIM_PASSED.value: 6,
    SchemaValidationOutcome.PASSED_AFTER_REPAIR.value: 7,
    SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value: 8,
    SchemaValidationOutcome.SCHEMA_UNENFORCEABLE.value: 9,
}


def derive_mode_from_records(
    enforcement_records: list[dict[str, Any]],
) -> str:
    """Derive the schema validator mode from per-attempt enforcement records.

    Pure function — no DB, no I/O.  Returns ``"lenient"`` if any record has
    a lenient-mode outcome (``LENIENT_VALIDATION_BYPASSED``), else ``"strict"``.

    When no records are provided, returns ``"lenient"`` (safe default — matches
    the hard default in ``resolve_schema_validator_mode``).
    """
    if not enforcement_records:
        return "lenient"
    for record in enforcement_records:
        if record.get("outcome") in _LENIENT_OUTCOMES:
            return "lenient"
    return "strict"


def derive_outcome_from_records(
    enforcement_records: list[dict[str, Any]],
) -> str:
    """Derive the run-level validation outcome from per-attempt enforcement records.

    Pure function — no DB, no I/O.  Returns the outcome with the lowest
    severity rank (most severe) across all records.  When no records are
    provided, returns ``"no_schema"``.
    """
    if not enforcement_records:
        return SchemaValidationOutcome.NO_SCHEMA.value
    worst_outcome = SchemaValidationOutcome.NO_SCHEMA.value
    worst_severity = len(_OUTCOME_SEVERITY) + 1
    for record in enforcement_records:
        outcome = record.get("outcome", "")
        severity = _OUTCOME_SEVERITY.get(outcome, len(_OUTCOME_SEVERITY))
        if severity < worst_severity:
            worst_severity = severity
            worst_outcome = outcome
    return worst_outcome


# ---------------------------------------------------------------------------
# Flip guard — lenient-to-strict advisory (FAR-902 D2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlipGuardVerdict:
    """The flip guard's advisory on whether to switch from lenient to strict.

    The guard NEVER mutates the mode — it only advises.  Changing the mode
    remains a separate, explicit, already-authorised operator action.

    Fields:

    - ``safe_to_flip``: True when zero lenient-mode warnings exist.
    - ``lenient_warning_count``: number of lenient-bypass records that would
      become hard failures in strict mode.
    - ``total_records``: total enforcement records examined.
    - ``affected_outcomes``: distinct outcome types from lenient-mode warnings.
    - ``advisory``: human-readable explanation of the verdict.
    """

    safe_to_flip: bool
    lenient_warning_count: int
    total_records: int
    affected_outcomes: tuple[str, ...]
    advisory: str


def compute_flip_guard_verdict(
    enforcement_records: list[dict[str, Any]],
) -> FlipGuardVerdict:
    """Compute the flip guard verdict from per-attempt enforcement records.

    Pure function — no DB, no I/O.  Examines enforcement records and
    determines whether switching from lenient to strict mode is safe.

    The verdict is ``safe_to_flip=True`` when zero lenient-mode warnings
    exist (no records with outcome ``LENIENT_VALIDATION_BYPASSED``).

    **This function NEVER mutates the mode.**  It only returns an advisory
    verdict.  Changing the mode remains a separate, explicit, already-
    authorised operator action (the existing settings/org-config path).
    """
    warning_outcomes: set[str] = set()
    warning_count = 0

    for record in enforcement_records:
        outcome = record.get("outcome", "")
        if outcome == SchemaValidationOutcome.LENIENT_VALIDATION_BYPASSED.value:
            warning_count += 1
            warning_outcomes.add(outcome)

    total = len(enforcement_records)
    safe = warning_count == 0

    if safe:
        advisory = (
            f"Safe to flip: {total} enforcement record(s) examined, "
            "zero lenient-mode warnings found.  No validation failures "
            "would become hard errors in strict mode."
        )
    else:
        advisory = (
            f"NOT safe to flip: {warning_count} of {total} enforcement "
            "record(s) are lenient-mode warnings that would become hard "
            f"failures in strict mode.  Outcomes: "
            f"{', '.join(sorted(warning_outcomes))}.  Resolve the "
            "underlying validation failures before switching to strict."
        )

    return FlipGuardVerdict(
        safe_to_flip=safe,
        lenient_warning_count=warning_count,
        total_records=total,
        affected_outcomes=tuple(sorted(warning_outcomes)),
        advisory=advisory,
    )
