"""FAR-899: Schema validation outcomes and repair loop for node output validation.

This module provides:
- ``SchemaValidationOutcome`` enum for structured validation result tracking
- ``SchemaRepairLoop`` for attempting model re-prompting on validation failure
- ``run_repair_loop`` — the repair orchestration extracted from node_runner
- ``format_error_summary`` — single error-summary formatter (DRY helper)

The repair loop operates in STRICT mode only. In lenient mode, validation
failures are recorded as warnings but the node does not fail.

# TODO(FAR-902): flip guard hooks in here (needs the runs.schema_validator_mode
# / schema_validation_outcome columns)
"""

from __future__ import annotations

import hashlib
import json
import logging
from enum import StrEnum
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import UnknownType
from jsonschema.exceptions import _Error as JsonschemaError

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
# FIX G: Malformed-schema guard — wraps compile + validate
# ---------------------------------------------------------------------------


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
      - ``allowed``: allowed enum values (if applicable, capped to 10 values
        of 50 chars each for injection hardening)

    FIX G: Malformed schemas (``UnknownType``, ``SchemaError``, etc.) are
    caught and returned as a single ``schema_error`` validation failure
    with pointer ``$`` rather than propagating and crashing the node.

    ``format`` keywords are advisory (no format_checker passed).
    Vendor ``x-*`` keywords are tolerated (default validator).
    External ``$ref`` rejection is handled by ``_is_safe_schema`` upstream.
    """
    try:
        validator = _compile_validator(schema)
    except (JsonschemaError, UnknownType) as exc:
        # FIX G: malformed schema → single validation failure, not a crash
        _log.warning(
            "schema_repair.malformed_schema",
            extra={"error": str(exc)[:200]},
        )
        return False, [{"pointer": "$", "constraint": "schema_error", "message": str(exc)[:200]}]

    errors: list[dict[str, Any]] = []

    try:
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
                # FIX F: cap allowed values to prevent prompt injection
                raw_allowed = error.validator_value
                capped = [str(v)[:50] for v in raw_allowed[:10]]
                err_info["allowed"] = capped

            errors.append(err_info)
    except (JsonschemaError, UnknownType) as exc:
        # FIX G: runtime validation failure on a schema that compiled OK
        # but raises during iteration (e.g. recursive schema blow-up)
        _log.warning(
            "schema_repair.schema_runtime_error",
            extra={"error": str(exc)[:200]},
        )
        return False, [{"pointer": "$", "constraint": "schema_error", "message": str(exc)[:200]}]

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# FIX I: Error summary helper (DRY - 4x duplicated in node_runner)
# ---------------------------------------------------------------------------


def format_error_summary(
    errors: list[dict[str, Any]],
    *,
    limit: int = 3,
) -> str:
    """Format a short semicolon-delimited error summary for messages/logs.

    This is the SINGLE source of truth for error-summary formatting.
    """
    return "; ".join(f"{e.get('pointer', '$')}: {e.get('constraint', 'unknown')}" for e in errors[:limit])


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

    FIX F: ``allowed`` values are capped to at most 10 values, each at most
    50 chars, before interpolation into the prompt.
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
            allowed = err["allowed"]
            if isinstance(allowed, list):
                capped = [str(v)[:50] for v in allowed[:10]]
                parts.append(f"  Allowed values: {capped}")
            else:
                parts.append(f"  Allowed values: {str(allowed)[:50]}")
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


# Maximum chars for the repair prompt error message (injection hardening).
_REPAIR_PROMPT_MAX_CHARS = 2048


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

    FIX D: budget=N must yield exactly N repair invocations. The loop checks
    ``is_exhausted`` BEFORE each iteration, and ``record_attempt`` only
    increments and checks untranslatable — the budget gate is in the loop,
    not in record_attempt.
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

        FIX D: No longer checks the budget cap — the loop's ``is_exhausted``
        handles that. This method only handles untranslatable detection.

        Returns:
            - ``REPAIR_ATTEMPTED`` if more attempts remain
            - ``PASSED_AFTER_REPAIR`` if validation now passes (caller sets this)
            - ``REPAIR_EXHAUSTED`` if untranslatable
        """
        self._attempt += 1

        # Check for untranslatable (same errors on consecutive attempts)
        if _is_untranslatable(self._last_errors, errors):
            self._consecutive_same_errors += 1
        else:
            self._consecutive_same_errors = 0

        self._last_errors = list(errors)

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

        return SchemaValidationOutcome.REPAIR_ATTEMPTED

    def build_repair_prompt(self, errors: list[dict[str, Any]]) -> str:
        """Build the injection-hardened repair prompt."""
        return _build_repair_prompt(
            errors=errors,
            schema_id=self.schema_id,
            attempt=self._attempt,
            max_attempts=self.budget,
        )


# ---------------------------------------------------------------------------
# FIX I: Repair orchestration (extracted from _validate_against_schema)
# ---------------------------------------------------------------------------


def run_repair_loop(
    data: Any,
    schema: dict[str, Any],
    *,
    budget: int,
    schema_id: str = "unknown",
    schema_version: int = 0,
    daily_spend_limit: float | None = None,
    current_spend: float = 0.0,
    repair_invoke_fn: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run the repair loop for a failed schema validation in strict mode.

    Called from ``_validate_against_schema`` after the initial validation
    fails. Builds repair prompts, invokes the backend, re-validates, and
    returns the final ``(outcome, errors)``.

    Returns:
        ``(outcome_value, errors)`` — the outcome is a SchemaValidationOutcome
        string value. The caller raises OutputSchemaValidationError when the
        outcome is terminal-failure.

    Raises:
        Nothing — all failures are encoded in the outcome value.
    """
    from json import JSONDecodeError

    from modulo.core.pipeline_engine.schema_repair import (
        SchemaRepairLoop,
        SchemaValidationOutcome,
    )

    if budget <= 0 or repair_invoke_fn is None:
        # No repair budget or no invoke function → terminal failure
        is_valid, errors = validate_against_schema(data, schema)
        if is_valid:
            return SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value, []
        return SchemaValidationOutcome.REPAIR_EXHAUSTED.value, errors

    # Check daily spend limit before attempting repair
    if daily_spend_limit is not None and current_spend >= daily_spend_limit:
        _log.warning(
            "schema_repair.skip_spend_limit",
            extra={"schema_id": schema_id, "spend": current_spend, "limit": daily_spend_limit},
        )
        is_valid, errors = validate_against_schema(data, schema)
        return SchemaValidationOutcome.REPAIR_EXHAUSTED.value, errors

    # Initial validation to get the errors
    is_valid, current_errors = validate_against_schema(data, schema)
    if is_valid:
        return SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value, []

    repair = SchemaRepairLoop(
        budget=budget,
        schema_id=schema_id,
        schema_version=schema_version,
    )

    last_outcome = SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED

    # FIX D: loop checks is_exhausted BEFORE attempting, so budget=N yields
    # exactly N invocations (is_exhausted is False until _attempt >= budget).
    while not repair.is_exhausted:
        outcome = repair.record_attempt(current_errors)
        if outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED:
            last_outcome = SchemaValidationOutcome.REPAIR_EXHAUSTED
            break

        # Build repair prompt and invoke
        prompt = repair.build_repair_prompt(current_errors)
        try:
            raw_output = repair_invoke_fn(prompt)
        except Exception:
            _log.exception(
                "schema_repair.invoke_failed",
                extra={"schema_id": schema_id, "attempt": repair.attempts_used},
            )
            last_outcome = SchemaValidationOutcome.REPAIR_EXHAUSTED
            break

        # Parse the raw output
        try:
            repaired_data = json.loads(raw_output)
        except (JSONDecodeError, ValueError):
            # Raw output is not valid JSON → terminal, no more repair
            last_outcome = SchemaValidationOutcome.NATIVE_DECODE_NOT_JSON
            break

        # Validate the repaired output
        is_valid, current_errors = validate_against_schema(repaired_data, schema)
        if is_valid:
            return SchemaValidationOutcome.PASSED_AFTER_REPAIR.value, []

        last_outcome = SchemaValidationOutcome.REPAIR_ATTEMPTED

    # Check terminal outcomes from break (invoke failure, bad JSON)
    if last_outcome in (
        SchemaValidationOutcome.NATIVE_DECODE_NOT_JSON,
        SchemaValidationOutcome.REPAIR_EXHAUSTED,
    ):
        return last_outcome.value, current_errors

    # Budget exhaustion: the loop exited because is_exhausted is True
    if repair.is_exhausted:
        return SchemaValidationOutcome.REPAIR_EXHAUSTED.value, current_errors

    # Should not reach here, but defensive
    return last_outcome.value, current_errors


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
