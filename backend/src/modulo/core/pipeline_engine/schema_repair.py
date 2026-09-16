"""FAR-899: Schema validation outcomes and repair loop for node output validation.

This module provides:
- ``SchemaValidationOutcome`` enum for structured validation result tracking
- ``SchemaRepairLoop`` for attempting model re-prompting on validation failure
- ``_query_recent_lenient_failures`` for the flip-guard threshold check

The repair loop operates in STRICT mode only. In lenient mode, validation
failures are recorded as warnings but the node does not fail.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema validation outcome enum (FAR-899 / FAR-902 persistence payload)
# ---------------------------------------------------------------------------


class SchemaValidationOutcome(StrEnum):
    """Outcome of schema validation on a node's output.

    Defined here for FAR-899; persisted in FAR-902.
    """

    NO_SCHEMA = "no_schema"
    NATIVE_DECODED_AND_VALIDATED = "native_decoded_and_validated"
    NATIVE_DECODE_FAILED = "native_decode_failed"
    NATIVE_DECODE_NOT_JSON = "native_decode_not_json"
    VERBATIM_PASSED = "verbatim_passed"
    POSTHOC_VALIDATION_FAILED = "posthoc_validation_failed"
    REPAIR_ATTEMPTED = "repair_attempted"
    PASSED_AFTER_REPAIR = "passed_after_repair"
    REPAIR_EXHAUSTED = "repair_exhausted"
    LENIENT_VALIDATION_BYPASSED = "lenient_validation_bypassed"
    SCHEMA_UNENFORCEABLE = "schema_unenforceable"


# ---------------------------------------------------------------------------
# Compiled validator cache (bounded LRU keyed by schema content hash)
# ---------------------------------------------------------------------------

_VALIDATOR_CACHE_MAX_SIZE = 256


@lru_cache(maxsize=_VALIDATOR_CACHE_MAX_SIZE)
def _get_compiled_validator(schema_key: str, schema_json: str) -> Draft202012Validator:
    """Return a compiled Draft202012Validator, cached by schema content hash.

    The cache key is a hash of the JSON-serialized schema. The actual schema
    dict is re-parsed from the stored JSON string on cache hits (lru_cache
    requires hashable args). This avoids recompilation on repeated calls
    with the same schema while keeping memory bounded.
    """
    schema = json.loads(schema_json)
    return Draft202012Validator(schema)


def _compile_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Get or compile a validator for the given schema, using the LRU cache."""
    schema_json = json.dumps(schema, sort_keys=True, ensure_ascii=True)
    schema_key = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()[:16]
    return _get_compiled_validator(schema_key, schema_json)


def _clear_validator_cache() -> None:
    """Clear the compiled validator cache (for testing)."""
    _get_compiled_validator.cache_clear()


# ---------------------------------------------------------------------------
# Core validation function
# ---------------------------------------------------------------------------

# Maximum chars for the repair prompt error message (injection hardening).
_REPAIR_PROMPT_MAX_CHARS = 2048


def validate_against_schema(
    data: Any,
    schema: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    """Validate *data* against *schema* using Draft202012Validator.

    Returns ``(is_valid, errors)`` where *errors* is a list of structured
    error dicts, each containing:
      - ``pointer``: JSON Pointer to the failing location
      - ``constraint``: the schema keyword that failed
      - ``expected``: expected type/value (if applicable)
      - ``actual``: actual type/value (if applicable)
      - ``allowed``: allowed enum values (if applicable)

    Unlike the old ``_validate_against_schema``, this works on ANY JSON value
    (dict, list, scalar) — there is no ``isinstance(dict)`` guard.

    ``format`` keywords are advisory (no format_checker passed).
    Vendor ``x-*`` keywords are tolerated (default validator).
    External ``$ref`` rejection is handled by ``_is_safe_schema`` upstream.
    """
    validator = _compile_validator(schema)
    errors: list[dict[str, Any]] = []

    for error in validator.iter_errors(data):
        pointer = error.json_path or "$"
        # Convert json_path notation to JSON Pointer format
        if pointer.startswith("$."):
            pointer = "/" + pointer[2:].replace(".", "/")
        elif pointer == "$":
            pointer = ""

        err_info: dict[str, Any] = {
            "pointer": pointer,
            "constraint": error.validator,
        }
        if error.message:
            err_info["message"] = error.message[:200]
        if error.validator_value is not None:
            err_info["validator_value"] = str(error.validator_value)[:100]
        if error.instance is not None:
            err_info["actual"] = str(error.instance)[:100]
        if error.validator == "enum" and error.validator_value is not None:
            err_info["allowed"] = error.validator_value

        errors.append(err_info)

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Repair prompt template (injection-hardened)
# ---------------------------------------------------------------------------

_REPAIR_PROMPT_TEMPLATE = """\
Schema validation failed for your output. Fix the errors below and return a corrected JSON output.

Schema ID: {schema_id}
Attempt: {attempt}/{max_attempts}

Validation errors:
{errors}

Return ONLY the corrected JSON output, nothing else."""


def _build_repair_prompt(
    errors: list[dict[str, Any]],
    schema_id: str,
    attempt: int,
    max_attempts: int,
) -> str:
    """Build an injection-hardened repair prompt from structured error metadata.

    Only structural metadata (JSON Pointer, constraint, expected, actual,
    allowed) is interpolated. Raw schema free-text (description, title,
    examples, default) is NEVER included. The message is capped at
    ``_REPAIR_PROMPT_MAX_CHARS``.
    """
    error_lines: list[str] = []
    for i, err in enumerate(errors, 1):
        parts = [f"  Error {i}:"]
        if err.get("pointer"):
            parts.append(f"  Location: {err['pointer']}")
        if err.get("constraint"):
            parts.append(f"  Constraint: {err['constraint']}")
        if err.get("expected"):
            parts.append(f"  Expected: {err['expected']}")
        if err.get("actual"):
            parts.append(f"  Actual: {err['actual']}")
        if err.get("allowed"):
            parts.append(f"  Allowed values: {err['allowed']}")
        error_lines.append("\n".join(parts))

    errors_text = "\n".join(error_lines)

    prompt = _REPAIR_PROMPT_TEMPLATE.format(
        schema_id=schema_id,
        attempt=attempt,
        max_attempts=max_attempts,
        errors=errors_text,
    )

    # Cap at max chars (injection hardening)
    if len(prompt) > _REPAIR_PROMPT_MAX_CHARS:
        prompt = prompt[: _REPAIR_PROMPT_MAX_CHARS - 50] + "\n... [truncated]"

    return prompt


# ---------------------------------------------------------------------------
# Repair loop
# ---------------------------------------------------------------------------

# Hard caps (config cannot exceed)
_REPAIR_BUDGET_HARD_CAP = 3
_REPAIR_BUDGET_DEFAULT = 1


def _clamp_repair_budget(requested: int) -> int:
    """Clamp the repair budget to [0, _REPAIR_BUDGET_HARD_CAP]."""
    return max(0, min(requested, _REPAIR_BUDGET_HARD_CAP))


def _is_untranslatable(
    errors_a: list[dict[str, Any]],
    errors_b: list[dict[str, Any]],
) -> bool:
    """Check if two error sets represent the same failures (untranslatable).

    Two consecutive attempts with the same JSON Pointer + constraint means
    the model cannot fix the issue. Returns True if ALL errors in B have a
    matching error in A (same pointer + same constraint).
    """
    if not errors_b:
        return False
    if not errors_a:
        return False

    a_pairs = {(e.get("pointer"), e.get("constraint")) for e in errors_a}
    b_pairs = {(e.get("pointer"), e.get("constraint")) for e in errors_b}

    # All B pairs must exist in A for "untranslatable"
    return b_pairs.issubset(a_pairs)


class SchemaRepairLoop:
    """Manages the schema repair loop for a single node invocation.

    Strict mode only. On validation failure, re-prompts the SAME backend
    with the validation error + schema. Budget defaults to 1, hard cap 3.
    """

    def __init__(
        self,
        *,
        budget: int = _REPAIR_BUDGET_DEFAULT,
        schema_id: str = "unknown",
        schema_version: int = 0,
    ) -> None:
        self.budget = _clamp_repair_budget(budget)
        self.schema_id = schema_id
        self.schema_version = schema_version
        self._attempt = 0
        self._last_errors: list[dict[str, Any]] = []
        self._consecutive_same_errors = 0

    @property
    def attempts_used(self) -> int:
        return self._attempt

    @property
    def is_exhausted(self) -> bool:
        return self._attempt >= self.budget

    def record_attempt(
        self,
        errors: list[dict[str, Any]],
    ) -> SchemaValidationOutcome:
        """Record a validation attempt and return the outcome.

        Returns:
            - ``REPAIR_ATTEMPTED`` if more attempts remain
            - ``PASSED_AFTER_REPAIR`` if validation now passes (caller sets this)
            - ``REPAIR_EXHAUSTED`` if budget exhausted or untranslatable
        """
        self._attempt += 1

        # Check for untranslatable (same errors on consecutive attempts)
        if _is_untranslatable(self._last_errors, errors):
            self._consecutive_same_errors += 1
        else:
            self._consecutive_same_errors = 0

        self._last_errors = errors

        if self._consecutive_same_errors >= 1:
            # Same errors on 2 consecutive attempts = untranslatable
            _log.warning(
                "schema_repair.untranslatable",
                extra={
                    "schema_id": self.schema_id,
                    "attempt": self._attempt,
                    "pointer": errors[0].get("pointer") if errors else None,
                    "constraint": errors[0].get("constraint") if errors else None,
                },
            )
            return SchemaValidationOutcome.REPAIR_EXHAUSTED

        if self._attempt >= self.budget:
            return SchemaValidationOutcome.REPAIR_EXHAUSTED

        return SchemaValidationOutcome.REPAIR_ATTEMPTED

    def build_repair_prompt(self, errors: list[dict[str, Any]]) -> str:
        """Build the injection-hardened repair prompt."""
        return _build_repair_prompt(
            errors=errors,
            schema_id=self.schema_id,
            attempt=self._attempt + 1,
            max_attempts=self.budget,
        )


# ---------------------------------------------------------------------------
# Flip guard: _query_recent_lenient_failures
# ---------------------------------------------------------------------------

# Threshold: ≤0.1% of trailing-72h lenient runs triggers the flip guard
_FLIP_GUARD_THRESHOLD = 0.001  # 0.1%
_FLIP_GUARD_WINDOW_HOURS = 72
_FLIP_GUARD_MAX_RUNS = 200


async def _query_recent_lenient_failures(
    session: AsyncSession,
    org_id: uuid.UUID | None,
    *,
    window_hours: int = _FLIP_GUARD_WINDOW_HOURS,
    max_runs: int = _FLIP_GUARD_MAX_RUNS,
) -> float:
    """Query the fraction of recent lenient-mode runs that had validation failures.

    Returns a float in [0.0, 1.0] representing the failure rate. Used by the
    flip guard to prevent switching to strict mode when too many recent runs
    would fail.

    The query covers trailing ``window_hours`` OR the last ``max_runs``
    lenient runs, whichever is smaller.

    Named ``_query_recent_lenient_failures`` so it is unit-testable in
    isolation.

    NOTE: This function queries the database for run statistics. In unit
    tests, mock this function to control the threshold check.
    """
    if org_id is None:
        return 0.0

    try:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import func, select

        from modulo.db.models.run import Run

        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)

        # Count total lenient runs in window
        total_q = (
            select(func.count())
            .select_from(Run)
            .where(
                Run.organisation_id == org_id,
                Run.created_at >= cutoff,
                Run.schema_validator_mode == "lenient",
            )
        )
        total_result = await session.execute(total_q)
        total_count = total_result.scalar() or 0

        if total_count == 0:
            return 0.0

        # Limit to max_runs window
        effective_count = min(total_count, max_runs)

        # Count failures among lenient runs in window
        failure_q = (
            select(func.count())
            .select_from(Run)
            .where(
                Run.organisation_id == org_id,
                Run.created_at >= cutoff,
                Run.schema_validator_mode == "lenient",
                Run.schema_validation_outcome.in_(
                    [
                        SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value,
                        SchemaValidationOutcome.REPAIR_EXHAUSTED.value,
                    ]
                ),
            )
        )
        failure_result = await session.execute(failure_q)
        failure_count = failure_result.scalar() or 0

        return min(failure_count / effective_count, 1.0)

    except Exception:
        _log.warning(
            "schema_repair.flip_guard_query_failed",
            extra={"org_id": str(org_id)},
            exc_info=True,
        )
        # Fail-open: return threshold + 1 to BLOCK the flip on query failure
        return _FLIP_GUARD_THRESHOLD + 1.0


# ---------------------------------------------------------------------------
# Repair budget helpers
# ---------------------------------------------------------------------------


def get_repair_budget(config_value: Any = None) -> int:
    """Return the effective repair budget, clamped to hard cap.

    Args:
        config_value: The operator-configured budget (from system_config).
                     None or invalid = default.

    Returns:
        Clamped budget in [0, _REPAIR_BUDGET_HARD_CAP].
    """
    if config_value is None:
        return _REPAIR_BUDGET_DEFAULT
    try:
        return _clamp_repair_budget(int(config_value))
    except (TypeError, ValueError):
        return _REPAIR_BUDGET_DEFAULT
