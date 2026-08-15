"""Guardrails — structured-credential boundary data-safety at the ingestion edge.

A guardrail is an :class:`~modulo.core.eval_engine.EvalDefinition` with
``eval_type="guardrail"``. Detection is DETERMINISTIC and PURE — only the
``regex`` and ``json_schema`` eval types may be used as guardrail detection;
``llm_judge`` and ``custom_function`` are never guardrail detection (the
engine raises on misrouting). T1 is vault/key-independent: no forensic
capture, no HMAC key, no fallback redactor.

Actions
-------
observe   compute + validate + discard + log would-block (shadow).
warn      log the violation; the run continues.
block     the run transitions to ``eval_failed`` (TERMINAL).
redact    masks-only field-scoped redaction at the ingestion edge.

Redaction
---------
Masks-only transform. The mask token is fixed and never derived from payload
content. Field paths are STATIC author config (never payload-derived) and
resolved with EXACT/ANCHOR key matching — substring matching is FORBIDDEN.
A built-in allowlist of never-touch system fields is always honoured.

Interception
------------
The guardrail pass runs at run-creation (the ingestion edge) BEFORE the run's
``input_payload`` is persisted — persisted state is post-redaction. The pass
is TWO-PHASE:

  1. Evaluate ALL bound guardrails against an immutable pre-act copy of the
     payload (no masks applied yet).
  2. Apply redaction masks in deterministic order on the result.

A block outcome raises :class:`GuardrailBlockedError` (an
``EvalBlockedError`` subclass) which the interception seam maps to a terminal
``eval_failed`` run.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalDefinition,
    EvalEngine,
    EvalResult,
    EvalType,
    GuardrailMisroutedError,
)


class GuardrailAction(StrEnum):
    """The behavioural action of a guardrail."""

    OBSERVE = "observe"
    WARN = "warn"
    BLOCK = "block"
    REDACT = "redact"


class FieldRedactionMode(StrEnum):
    """Per-field redaction policy mode. All are masks-only — never destroy."""

    TRANSFORM = "transform"
    DROP = "drop"
    BLOCK = "block"


# Fixed mask token — never derived from payload content, never reversible.
REDACTION_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022"

# System fields a guardrail may NEVER touch. These are author-independent and
# are enforced regardless of what an author configures.
GUARDRAIL_NEVER_TOUCH_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "pipeline_id",
        "snapshot_id",
        "organisation_id",
        "account_id",
        "trigger_id",
        "work_item_id",
        "langgraph_thread_id",
        "run_number",
        "input_hash",
        "is_replay",
        "parent_run_id",
        "rate_limit_key",
        "owner_team_id",
        "variant_group_id",
        "feedback_correction",
    }
)

# Default cap of guardrails bound per pipeline node (item 7). Configurable via
# guardrail config, never below this floor's spirit (0 = feature off).
DEFAULT_MAX_GUARDRAILS_PER_NODE = 8

# Only deterministic, pure eval types may serve as guardrail detection.
GUARDRAIL_DETECTION_TYPES: frozenset[str] = frozenset({EvalType.REGEX, EvalType.JSON_SCHEMA})

# Guardrail eval definitions may never carry a retry failure behaviour — a
# guardrail block is terminal and retries are excluded by design (item 5).
GUARDRAIL_FORBIDDEN_FAILURE_BEHAVIOURS: frozenset[str] = frozenset({"retry"})


class GuardrailConfigError(ValueError):
    """Raised when a guardrail definition is malformed."""


class GuardrailBlockedError(EvalBlockedError):
    """A guardrail blocked the run at the ingestion edge — terminal eval_failed."""


class FieldRedactionPolicy(BaseModel):
    """Static field-path redaction policy (author config, NEVER payload-derived)."""

    path: str = Field(min_length=1)
    mode: FieldRedactionMode = FieldRedactionMode.TRANSFORM


class GuardrailConfig(BaseModel):
    """The ``config_json`` shape of an eval_type='guardrail' definition.

    ``interception_point`` is always ``"input"`` in T1 (the ingestion edge).
    ``redaction`` is a list of static field-path policies applied by
    redact-action guardrails. ``required_capabilities`` optionally declares a
    conformance claim (see :func:`derive_conformance_state`); empty means no
    conformance claim.
    """

    interception_point: Literal["input"] = "input"
    action: GuardrailAction = GuardrailAction.OBSERVE
    redaction: list[FieldRedactionPolicy] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    max_guardrails_per_node: int = Field(default=DEFAULT_MAX_GUARDRAILS_PER_NODE, ge=0)

    @classmethod
    def from_eval_config(cls, config: dict[str, Any]) -> GuardrailConfig:
        """Parse + validate a guardrail eval definition's ``config_json``."""
        return cls.model_validate(config)


@dataclass
class RedactionEntry:
    """One applied (or skipped) redaction action during the pass."""

    path: str
    mode: str
    applied: bool
    reason: str = ""


@dataclass
class GuardrailPassResult:
    """Outcome of a two-phase guardrail pass at the ingestion edge."""

    results: list[EvalResult] = field(default_factory=list)
    redactions: list[RedactionEntry] = field(default_factory=list)
    observed_only: bool = True


@dataclass
class GuardrailInterceptionOutcome:
    """Non-raising interception outcome — used by the run-creation seam.

    ``payload`` is the post-redaction payload the caller persists (persisted
    state is post-redaction). ``blocked`` is True when a block-action guardrail
    (or a block-mode redaction policy) fired; ``block_message`` carries the
    terminal reason for the ``eval_failed`` run.
    """

    payload: dict[str, Any]
    results: list[EvalResult] = field(default_factory=list)
    redactions: list[RedactionEntry] = field(default_factory=list)
    blocked: bool = False
    block_message: str = ""
    blocking_eval_name: str = ""


# ---------------------------------------------------------------------------
# Static field-path resolution (EXACT/ANCHOR matching — substring FORBIDDEN)
# ---------------------------------------------------------------------------


def _split_path(path: str) -> list[str]:
    return [segment for segment in path.split(".") if segment]


def resolve_static_path(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Resolve *path* against *payload* with exact key matching.

    Returns ``(found, value)``. ``found`` is False when any segment is absent.
    Segment matching is an exact key lookup — never a substring match. A path
    with an empty/blank segment is invalid and resolves to not-found.
    """
    segments = _split_path(path)
    if not segments:
        return False, None
    current: Any = payload
    for segment in segments:
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def set_static_path(payload: dict[str, Any], path: str, value: Any) -> bool:
    """Set *path* to *value* in a shallow-copied caller-owned dict.

    Returns True when the path existed and was updated. Missing intermediate
    segments are NEVER created — a guardrail must not materialise paths that
    the author's static config did not intend to exist.
    """
    segments = _split_path(path)
    if not segments:
        return False
    current = payload
    for segment in segments[:-1]:
        if not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
    if not isinstance(current, dict) or segments[-1] not in current:
        return False
    current[segments[-1]] = value
    return True


# ---------------------------------------------------------------------------
# Redaction (masks-only)
# ---------------------------------------------------------------------------


def apply_redaction_masks(
    payload: dict[str, Any],
    policies: Sequence[FieldRedactionPolicy],
    *,
    allowlist: Iterable[str] = GUARDRAIL_NEVER_TOUCH_FIELDS,
    raise_on_block: bool = False,
    guardrail_name: str = "guardrail",
) -> tuple[dict[str, Any], list[RedactionEntry]]:
    """Apply *policies* to a deep copy of *payload* (masks-only).

    Returns ``(redacted_payload, entries)``. Deterministic order: policies are
    applied in the order given (authors control ordering). Exact/anchor path
    resolution only. Allowlisted paths are skipped and recorded.

    A ``drop`` policy removes the key; a ``block`` policy raises
    :class:`GuardrailBlockedError` when the path resolves to a non-empty value
    (the field is evidence of the guarded condition) and *raise_on_block* is
    True.
    """
    if not payload:
        return copy.deepcopy(payload), []
    redacted: dict[str, Any] = copy.deepcopy(payload)
    entries: list[RedactionEntry] = []
    allow = frozenset(allowlist)
    for policy in policies:
        path = policy.path
        top_segment = _split_path(path)[0] if _split_path(path) else ""
        if top_segment in allow:
            entries.append(RedactionEntry(path=path, mode=policy.mode.value, applied=False, reason="allowlist"))
            continue
        found, value = resolve_static_path(redacted, path)
        if not found:
            entries.append(RedactionEntry(path=path, mode=policy.mode.value, applied=False, reason="field-absent"))
            continue
        if policy.mode == FieldRedactionMode.BLOCK:
            present = value is not None and (not isinstance(value, (str, list, dict)) or bool(value))
            if present and raise_on_block:
                raise GuardrailBlockedError(guardrail_name, f"blocked field {path!r} present in payload")
            entries.append(
                RedactionEntry(
                    path=path, mode=policy.mode.value, applied=present, reason="present" if present else "field-absent"
                )
            )
            continue
        if policy.mode == FieldRedactionMode.DROP:
            redacted = _delete_static_path(redacted, path)
            entries.append(RedactionEntry(path=path, mode=policy.mode.value, applied=True, reason="dropped"))
            continue
        # transform (default): masks-only
        set_static_path(redacted, path, REDACTION_MASK)
        entries.append(RedactionEntry(path=path, mode=policy.mode.value, applied=True, reason="masked"))
    return redacted, entries


def _delete_static_path(payload: dict[str, Any], path: str) -> dict[str, Any]:
    segments = _split_path(path)
    if not segments:
        return payload
    current = payload
    for segment in segments[:-1]:
        if not isinstance(current, dict) or segment not in current:
            return payload
        current = current[segment]
    if isinstance(current, dict) and segments[-1] in current:
        del current[segments[-1]]
    return payload


# ---------------------------------------------------------------------------
# Detection (deterministic, pure — regex | json_schema only)
# ---------------------------------------------------------------------------


def _resolve_top_level_detection(config: dict[str, Any]) -> str:
    detection_type = config.get("type")
    if detection_type is None:
        # Legacy lenient form: a top-level ``schema`` dict with no declared
        # ``type`` implies json_schema; otherwise default to regex. A DECLARED
        # type outside the allowed set is returned as-is so validation fails
        # closed — it is never silently downgraded to another detector.
        if isinstance(config.get("schema"), dict):
            return str(EvalType.JSON_SCHEMA)
        return str(EvalType.REGEX)
    return str(detection_type)


def _resolve_detection(eval_def: EvalDefinition) -> tuple[str, dict[str, Any]]:
    """Resolve a guardrail's detection declaration to (type, effective config).

    Detection may be declared either flattened (top-level ``type`` /
    ``pattern`` / ``schema`` / ``field``) or inside a ``detection`` envelope
    (``{"detection": {"type": "regex", "pattern": ..., "field": ...}}``) as
    documented in the PRD §8.17. The envelope is authoritative when present;
    the effective config is the flattened merge so the pure eval helpers read
    a single shape.

    An envelope that DECLARES a detection type outside the allowed set fails
    closed — validation rejects it, never silently downgrading to another
    detector. An envelope without a ``type`` key falls back to top-level
    resolution (its pattern/schema/field keys still merge) so a valid
    top-level detection is not silently no-op'd by a nested envelope. A
    top-level ``schema`` dict with no ``type`` implies json_schema (legacy
    lenient form).
    """
    config = eval_def.config
    envelope = config.get("detection")
    if isinstance(envelope, dict):
        env_type = envelope.get("type")
        merged = {**config, **envelope}
        if env_type in GUARDRAIL_DETECTION_TYPES:
            return str(env_type), merged
        if env_type is not None:
            return str(env_type), merged
        return _resolve_top_level_detection(config), merged
    return _resolve_top_level_detection(config), config


def _validate_guardrail_definition(eval_def: EvalDefinition) -> GuardrailConfig:
    if eval_def.eval_type != EvalType.GUARDRAIL:
        raise GuardrailMisroutedError(eval_def.name)
    if eval_def.failure_behaviour in GUARDRAIL_FORBIDDEN_FAILURE_BEHAVIOURS:
        raise GuardrailConfigError(f"Guardrail {eval_def.name!r} must never carry failure_behaviour='retry'")
    detection_type, effective_config = _resolve_detection(eval_def)
    if detection_type not in GUARDRAIL_DETECTION_TYPES:
        raise GuardrailConfigError(
            f"Guardrail {eval_def.name!r} must use regex or json_schema detection (got config {eval_def.config!r})"
        )
    if detection_type == EvalType.JSON_SCHEMA:
        if not isinstance(effective_config.get("schema"), dict):
            raise GuardrailConfigError(
                f"Guardrail {eval_def.name!r} json_schema detection requires a 'schema' dict "
                f"(got config {eval_def.config!r})"
            )
    elif not effective_config.get("pattern") or not effective_config.get("field"):
        # Fail-closed at validation: a block/redact guardrail whose detector
        # cannot run (missing pattern/field) must not silently pass through.
        raise GuardrailConfigError(
            f"Guardrail {eval_def.name!r} regex detection requires non-empty 'pattern' and 'field' "
            f"(got config {eval_def.config!r})"
        )
    return GuardrailConfig.from_eval_config(eval_def.config)


def _interpret_violation(detection_type: str, result: EvalResult) -> bool:
    """Interpret a raw detection eval result as a guardrail violation.

    Guardrail detection reuses the pure eval helpers, whose ``passed``
    semantics differ per type:

    * ``regex``      — ``passed`` means the guarded pattern MATCHED. For a
      deny-style guardrail that is exactly the violation (credential present).
    * ``json_schema`` — ``passed`` means the payload validated. A validation
      failure IS the violation.

    The guardrail layer therefore inverts regex results (match = violation)
    and passes json_schema results through (failed = violation).
    """
    if detection_type == EvalType.JSON_SCHEMA:
        return not result.passed
    return result.passed


def _sanitise_guardrail_detail(
    detection_type: str,
    effective_config: dict[str, Any],
    result: EvalResult,
) -> EvalResult:
    """Return *result* with a value-free detail for a json_schema violation.

    jsonschema's ``ValidationError.message`` embeds the raw offending value
    (``'SECRET_ABC12345' is not of type 'boolean'``). The no-raw-persist
    contract says guardrail detail is count-only / pattern-descriptive — NEVER
    raw payload — so a json_schema failure detail is rewritten to a fixed,
    field-descriptive descriptor before it can reach persisted columns
    (``eval_results.detail`` and ``runs.error_detail``). Regex details are
    already pattern-descriptive (no payload) and are left untouched.
    """
    if detection_type != EvalType.JSON_SCHEMA or result.passed:
        return result
    field = effective_config.get("field") or ""
    detail = f"json_schema validation failed on field {field!r}" if field else "json_schema validation failed"
    return result.model_copy(update={"detail": detail})


def evaluate_guardrails(
    engine: EvalEngine,
    definitions: Sequence[EvalDefinition],
    payload: dict[str, Any],
    *,
    raise_on_block: bool = True,
) -> list[EvalResult]:
    """Evaluate ALL *definitions* against *payload* (phase one).

    Pure detection only. A violation on a ``block``-action guardrail raises
    :class:`GuardrailBlockedError` (terminal) when *raise_on_block* is True.
    ``warn``/``observe`` guardrails never raise. Guardrail detection reuses
    the pure eval helpers by building a transient regex/json_schema
    ``EvalDefinition`` mirror of the guardrail.
    """
    results: list[EvalResult] = []
    violations: list[tuple[EvalDefinition, EvalResult]] = []
    for eval_def in definitions:
        _validate_guardrail_definition(eval_def)
        detection_type, effective_config = _resolve_detection(eval_def)
        mirrored = eval_def.model_copy(
            update={
                "eval_type": EvalType(detection_type),
                "failure_behaviour": "warn",  # block semantics are guardrail-owned
                "config": effective_config,
            }
        )
        result = engine.evaluate(payload, mirrored)
        result = _sanitise_guardrail_detail(detection_type, effective_config, result)
        results.append(result)
        if _interpret_violation(detection_type, result) and eval_def.config.get("action") == GuardrailAction.BLOCK:
            violations.append((eval_def, result))
    if violations and raise_on_block:
        first_def, first_result = violations[0]
        raise GuardrailBlockedError(first_def.name, first_result.detail)
    return results


# ---------------------------------------------------------------------------
# Two-phase pass
# ---------------------------------------------------------------------------


def run_guardrail_pass(
    engine: EvalEngine,
    definitions: Sequence[EvalDefinition],
    payload: dict[str, Any],
) -> GuardrailPassResult:
    """Two-phase guardrail pass over an immutable pre-act payload (raising).

    Phase one evaluates every bound guardrail against an unmodified copy
    (block-action failures raise before any mask is applied). Phase two applies
    redaction masks in deterministic policy order. The caller persists the
    redacted payload — persisted state is post-redaction.

    This is the raising variant (used where the caller wants an exception).
    The run-creation seam uses :func:`run_interception_pass` (non-raising).
    """
    if not definitions:
        return GuardrailPassResult()
    outcome = run_interception_pass(engine, definitions, payload, detection_only=False)
    if outcome.blocked:
        raise GuardrailBlockedError(outcome.blocking_eval_name or "<guardrail>", outcome.block_message)
    return GuardrailPassResult(
        results=outcome.results,
        redactions=outcome.redactions,
        observed_only=not any(not r.passed for r in outcome.results),
    )


# ---------------------------------------------------------------------------
# Conformance (three-state for block-action guardrails only)
# ---------------------------------------------------------------------------

ConformanceState = Literal["present", "absent", "unknown"]


@dataclass(frozen=True)
class ConformanceDerivation:
    """Three-state conformance derivation for a block-action guardrail.

    ``present``  — every required capability is confirmed present on a
                   registered surface.
    ``absent``   — at least one required capability is confirmed absent.
    ``unknown``  — at least one required capability could not be read.

    Enforcement is fail-closed for block-action guardrails: confirmed-absent
    AND unknown both block. observe/warn guardrails are advisory and NEVER
    fail-closed on conformance.
    """

    state: ConformanceState
    missing: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    claimed: bool = False


def derive_conformance_state(
    required_capabilities: Sequence[str],
    registered: dict[str, bool | None],
) -> ConformanceDerivation:
    """Derive the conformance state for *required_capabilities*.

    *registered* maps a capability name to its confirmed state on the
    registered surfaces (connector scope table, EnvironmentProfile
    capabilities, agent required capabilities): True = confirmed present,
    False = confirmed absent, None = unreadable/unknown.

    An empty *required_capabilities* yields no conformance claim
    (``claimed=False``, state "na" via ``present`` with no claims).
    """
    if not required_capabilities:
        return ConformanceDerivation(state="present", claimed=False)
    missing: list[str] = []
    unreadable: list[str] = []
    for capability in required_capabilities:
        confirmed = registered.get(capability)
        if confirmed is True:
            continue
        if confirmed is False:
            missing.append(capability)
        else:
            unreadable.append(capability)
    if missing:
        return ConformanceDerivation(state="absent", missing=tuple(missing), unreadable=tuple(unreadable), claimed=True)
    if unreadable:
        return ConformanceDerivation(state="unknown", missing=(), unreadable=tuple(unreadable), claimed=True)
    return ConformanceDerivation(state="present", claimed=True)


# ---------------------------------------------------------------------------
# Interception (non-raising — used by the run-creation seam)
# ---------------------------------------------------------------------------


def run_interception_pass(
    engine: EvalEngine,
    definitions: Sequence[EvalDefinition],
    payload: dict[str, Any],
    *,
    detection_only: bool = False,
) -> GuardrailInterceptionOutcome:
    """Non-raising two-phase guardrail pass for the ingestion edge.

    Phase one evaluates every bound guardrail against an immutable pre-act
    copy (block violations are recorded, never raised). Phase two applies
    redaction masks in deterministic policy order to redact-action
    guardrails. A block-mode redaction policy firing is also recorded, not
    raised.

    *detection_only* (replays) skips both the block decision and the
    redaction act — replays are detection-only (item 10).
    """
    if not definitions:
        return GuardrailInterceptionOutcome(payload=dict(payload), results=[])
    pre_act = copy.deepcopy(payload)
    results = evaluate_guardrails(engine, definitions, pre_act, raise_on_block=False)

    blocked: bool = False
    block_message: str = ""
    blocking_eval_name: str = ""
    for eval_def, result in zip(definitions, results, strict=True):
        detection_type, _ = _resolve_detection(eval_def)
        if _interpret_violation(detection_type, result) and eval_def.config.get("action") == GuardrailAction.BLOCK:
            blocked = True
            block_message = f"Guardrail {eval_def.name!r} blocked: {result.detail}"
            blocking_eval_name = eval_def.name
            break

    if detection_only:
        return GuardrailInterceptionOutcome(payload=pre_act, results=results, blocked=False)

    redacted: dict[str, Any] = pre_act
    entries: list[RedactionEntry] = []
    for eval_def in definitions:
        cfg = _validate_guardrail_definition(eval_def)
        if cfg.action != GuardrailAction.REDACT or not cfg.redaction:
            continue
        try:
            redacted, batch_entries = apply_redaction_masks(
                redacted,
                cfg.redaction,
                raise_on_block=True,
                guardrail_name=eval_def.name,
            )
            entries.extend(batch_entries)
        except GuardrailBlockedError as exc:
            if not blocked:
                blocked = True
                block_message = str(exc)
                blocking_eval_name = eval_def.name
    return GuardrailInterceptionOutcome(
        payload=redacted,
        results=results,
        redactions=entries,
        blocked=blocked,
        block_message=block_message,
        blocking_eval_name=blocking_eval_name,
    )


# ---------------------------------------------------------------------------
# DB row → engine DTO
# ---------------------------------------------------------------------------


def to_engine_definition(db_row: Any) -> EvalDefinition:
    """Build an engine ``EvalDefinition`` DTO from a DB ``eval_definitions`` row.

    The interception seam runs inside ``db.crud.run.create_run``; this keeps
    the mapping localised so the DB layer never reaches into the engine's
    internals.
    """
    return EvalDefinition(
        id=db_row.id,
        org_id=db_row.organisation_id,
        pipeline_id=db_row.pipeline_id,
        node_id=str(db_row.node_id) if db_row.node_id else None,
        name=db_row.name,
        eval_type=EvalType(db_row.eval_type),
        config=dict(db_row.config_json or {}),
        failure_behaviour=db_row.failure_behaviour,
        pass_threshold=float(db_row.pass_threshold) if db_row.pass_threshold is not None else None,
        suite_id=db_row.suite_id,
    )


__all__ = [
    "DEFAULT_MAX_GUARDRAILS_PER_NODE",
    "GUARDRAIL_DETECTION_TYPES",
    "GUARDRAIL_FORBIDDEN_FAILURE_BEHAVIOURS",
    "GUARDRAIL_NEVER_TOUCH_FIELDS",
    "REDACTION_MASK",
    "ConformanceDerivation",
    "ConformanceState",
    "FieldRedactionMode",
    "FieldRedactionPolicy",
    "GuardrailAction",
    "GuardrailBlockedError",
    "GuardrailConfig",
    "GuardrailConfigError",
    "GuardrailInterceptionOutcome",
    "GuardrailMisroutedError",
    "GuardrailPassResult",
    "RedactionEntry",
    "apply_redaction_masks",
    "derive_conformance_state",
    "evaluate_guardrails",
    "resolve_static_path",
    "run_guardrail_pass",
    "run_interception_pass",
    "set_static_path",
    "to_engine_definition",
]
