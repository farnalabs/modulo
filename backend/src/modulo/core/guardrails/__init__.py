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

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalDefinition,
    EvalEngine,
    EvalResult,
    EvalType,
    GuardrailMisroutedError,
)

_log = logging.getLogger(__name__)


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

# Per-guardrail hard timeout for detection (item 7) — each guardrail's
# detection is bounded with ``asyncio.wait_for`` so a single pathological
# regex can never hold the ingestion edge hostage. Configurable via guardrail
# config.
DEFAULT_GUARDRAIL_TIMEOUT_SECONDS = 2.0

# Bounded-payload budget checked BEFORE guardrail evaluation (item 7) — a
# guardrail pass must never run regex/json_schema detection over an unbounded
# payload (ReDoS amplification guard). Over-budget fails CLOSED for
# block/redact guardrails and log-and-continues for observe/warn.
DEFAULT_MAX_GUARDRAIL_PAYLOAD_BYTES = 1_000_000

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
    guardrail_timeout_seconds: float = Field(default=DEFAULT_GUARDRAIL_TIMEOUT_SECONDS, gt=0)

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


@dataclass(frozen=True)
class GuardrailSkip:
    """A bound guardrail that the interception pass could not evaluate.

    Item 10 (snapshot & replay residual): a guardrail referenced by a
    snapshot pin whose live row no longer exists (soft-deleted) is SKIPPED —
    never a run failure. The seam writes an audit event and raises an
    enforcement-gap alert for each skip. ``reason`` is the skip enum:
    ``"soft_deleted"`` (pinned row gone) — later kinds may extend it.
    """

    name: str
    reason: str = "soft_deleted"
    detail: str = ""


@dataclass
class GuardrailInterceptionOutcome:
    """Non-raising interception outcome — used by the run-creation seam.

    ``payload`` is the post-redaction payload the caller persists (persisted
    state is post-redaction). ``blocked`` is True when a block-action guardrail
    (or a block-mode redaction policy, or a fail-closed mechanism error on a
    block/redact guardrail) fired; ``block_message`` carries the terminal
    reason for the ``eval_failed`` run. ``skipped`` lists guardrails the pass
    could not evaluate (e.g. a soft-deleted pinned guardrail).
    """

    payload: dict[str, Any]
    results: list[EvalResult] = field(default_factory=list)
    redactions: list[RedactionEntry] = field(default_factory=list)
    blocked: bool = False
    block_message: str = ""
    blocking_eval_name: str = ""
    skipped: list[GuardrailSkip] = field(default_factory=list)


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
            dropped = _delete_static_path(redacted, path)
            entries.append(
                RedactionEntry(
                    path=path,
                    mode=policy.mode.value,
                    applied=dropped,
                    reason="dropped" if dropped else "field-absent",
                )
            )
            continue
        # transform (default): masks-only
        set_static_path(redacted, path, REDACTION_MASK)
        entries.append(RedactionEntry(path=path, mode=policy.mode.value, applied=True, reason="masked"))
    return redacted, entries


def _delete_static_path(payload: dict[str, Any], path: str) -> bool:
    """Delete *path* from a caller-owned dict (mirrors :func:`set_static_path`).

    Returns ``True`` when a key was actually removed, ``False`` when the path
    was absent or could not be resolved. The dict is mutated in place (callers
    always pass a deep copy), but the boolean return makes the outcome explicit
    instead of always returning the input payload.
    """
    segments = _split_path(path)
    if not segments:
        return False
    current = payload
    for segment in segments[:-1]:
        if not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
    if isinstance(current, dict) and segments[-1] in current:
        del current[segments[-1]]
        return True
    return False


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


def _detect_one(
    engine: EvalEngine,
    pre_act: dict[str, Any],
    eval_def: EvalDefinition,
) -> EvalResult:
    """Validate + mirror + evaluate ONE guardrail against *pre_act*.

    Shared by the sync pass (:func:`evaluate_guardrails`) and the bounded
    async pass (:func:`run_interception_pass_async`, which invokes this in a
    worker thread under ``asyncio.wait_for``). Raises
    :class:`GuardrailConfigError` / :class:`GuardrailMisroutedError` on a
    malformed guardrail — the caller decides fail-closed vs log-and-continue.
    """
    _validate_guardrail_definition(eval_def)
    detection_type, effective_config = _resolve_detection(eval_def)
    mirrored = eval_def.model_copy(
        update={
            "eval_type": EvalType(detection_type),
            "failure_behaviour": "warn",  # block semantics are guardrail-owned
            "config": effective_config,
        }
    )
    result = engine.evaluate(pre_act, mirrored)
    return _sanitise_guardrail_detail(detection_type, effective_config, result)


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
        result = _detect_one(engine, payload, eval_def)
        results.append(result)
        detection_type, _ = _resolve_detection(eval_def)
        if _interpret_violation(detection_type, result) and eval_def.config.get("action") == GuardrailAction.BLOCK:
            violations.append((eval_def, result))
    if violations and raise_on_block:
        first_def, first_result = violations[0]
        raise GuardrailBlockedError(first_def.name, first_result.detail)
    return results


def _resolve_action(eval_def: EvalDefinition) -> GuardrailAction:
    """Best-effort action resolution for mechanism-error decisions.

    A malformed/unknown action string resolves to OBSERVE — matching the
    pre-existing seam behaviour (only a declared block/redact action is
    treated as guarding) so a malformed row never silently becomes a block.
    """
    try:
        return GuardrailAction(eval_def.config.get("action") or GuardrailAction.OBSERVE.value)
    except ValueError:
        return GuardrailAction.OBSERVE


def _detect_block(
    definitions: Sequence[EvalDefinition],
    results: Sequence[EvalResult],
) -> tuple[bool, str, str]:
    """Determine the block decision from aligned (definitions, results)."""
    for eval_def, result in zip(definitions, results, strict=True):
        detection_type, _ = _resolve_detection(eval_def)
        if _interpret_violation(detection_type, result) and eval_def.config.get("action") == GuardrailAction.BLOCK:
            return True, f"Guardrail {eval_def.name!r} blocked: {result.detail}", eval_def.name
    return False, "", ""


def _apply_redaction_phase(
    definitions: Sequence[EvalDefinition],
    redacted: dict[str, Any],
) -> tuple[dict[str, Any], list[RedactionEntry], bool, str, str]:
    """Phase two — apply redaction masks to redact-action guardrails.

    Returns ``(redacted, entries, blocked, block_message, blocking_eval_name)``.
    A block-mode redaction policy firing records a block (never raises).
    """
    entries: list[RedactionEntry] = []
    blocked: bool = False
    block_message: str = ""
    blocking_eval_name: str = ""
    for eval_def in definitions:
        try:
            cfg = _validate_guardrail_definition(eval_def)
        except (GuardrailConfigError, GuardrailMisroutedError, ValidationError):
            # A malformed guardrail already failed at the evaluation stage
            # (mechanism error handled by the caller: fail-closed for
            # block/redact, log-and-continue for observe/warn). Re-validating
            # here must NOT re-raise and kill the whole pass — skip the
            # redaction phase for it so the caller's decided outcome stands.
            # ValidationError covers the shape-level failures (a bad
            # ``action`` value, a non-positive timeout, a malformed redaction
            # rule) that ``GuardrailConfig.from_eval_config`` raises when the
            # guardrail-rule checks above pass.
            _log.warning(
                "guardrails.redaction_phase_skip",
                extra={"guardrail": eval_def.name},
            )
            continue
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
    return redacted, entries, blocked, block_message, blocking_eval_name


def _mechanism_fail_result(eval_def: EvalDefinition, reason: str) -> EvalResult:
    """A synthetic failed eval result for a log-and-continue mechanism error.

    Never a raw payload in the detail — only descriptive text (the no-raw
    persist contract).
    """
    return EvalResult(
        run_id=uuid.uuid4(),
        node_id=eval_def.node_id or "",
        eval_id=eval_def.id,
        passed=False,
        score=0.0,
        detail=f"guardrail {eval_def.name!r} mechanism error: {reason}",
    )


# ---------------------------------------------------------------------------
# Item 7 — cap enforcement + bounded evaluation
# ---------------------------------------------------------------------------


def resolve_guardrail_cap(definitions: Sequence[EvalDefinition]) -> int:
    """Resolve the effective per-node guardrail cap (0 = feature off).

    The cap is org-configurable via guardrail config-as-code: every applied row
    carries ``max_guardrails_per_node`` in its ``config_json`` (mirrored from
    the org's ``GuardrailConfigSet`` at apply time). The effective cap is the
    MAXIMUM declared across the bound rows — a single row carrying 0 turns the
    cap OFF for the whole pipeline. Falls back to the module constant when no
    row declares it.
    """
    declared = [d.config.get("max_guardrails_per_node") for d in definitions]
    values = [v for v in declared if isinstance(v, int) and not isinstance(v, bool) and v >= 0]
    if 0 in values:
        return 0
    return max(values, default=DEFAULT_MAX_GUARDRAILS_PER_NODE)


def guardrail_cap_violation(definitions: Sequence[EvalDefinition]) -> str | None:
    """Return a description of the first per-node cap violation, or None.

    Org-level guardrails (``node_id IS NULL``) bind to EVERY node; a node-bound
    guardrail binds to its node. A node's effective count is org-level count +
    its own rows. ``cap <= 0`` (feature off) never violates.
    """
    cap = resolve_guardrail_cap(definitions)
    if cap <= 0:
        return None
    org_count = sum(1 for d in definitions if not d.node_id)
    by_node: dict[str, int] = {}
    for d in definitions:
        if d.node_id:
            by_node[d.node_id] = by_node.get(d.node_id, 0) + 1
    if org_count > cap:
        return f"org-level guardrail count {org_count} exceeds per-node cap {cap}"
    for nid, count in sorted(by_node.items()):
        if org_count + count > cap:
            return f"node {nid!r} binds {org_count + count} guardrails (cap {cap})"
    return None


def resolve_guardrail_timeout(definitions: Sequence[EvalDefinition]) -> float:
    """Resolve the effective per-guardrail detection timeout (seconds).

    Same configurable mechanism as the cap — the maximum declared
    ``guardrail_timeout_seconds`` across the bound rows, falling back to
    :data:`DEFAULT_GUARDRAIL_TIMEOUT_SECONDS`.
    """
    declared = [d.config.get("guardrail_timeout_seconds") for d in definitions]
    values = [v for v in declared if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
    return float(max(values, default=DEFAULT_GUARDRAIL_TIMEOUT_SECONDS))


def check_payload_within_budget(
    payload: dict[str, Any],
    max_bytes: int = DEFAULT_MAX_GUARDRAIL_PAYLOAD_BYTES,
) -> bool:
    """True when *payload*'s serialised size is within *max_bytes*.

    Best-effort: an unserialisable payload is allowed through (evaluation will
    handle it); the size guard is a DoS-amplification budget, not a validation
    gate.
    """
    if max_bytes <= 0 or not payload:
        return True
    try:
        return len(json.dumps(payload, separators=(",", ":"), default=str)) <= max_bytes
    except (TypeError, ValueError):
        return True


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
    """Non-raising two-phase guardrail pass for the ingestion edge (sync).

    Phase one evaluates every bound guardrail against an immutable pre-act
    copy (block violations are recorded, never raised). Phase two applies
    redaction masks in deterministic policy order to redact-action
    guardrails. A block-mode redaction policy firing is also recorded, not
    raised.

    *detection_only* (replays) skips both the block decision and the
    redaction act — replays are detection-only (item 10).

    NOTE: this unbounded sync variant is the test/contract surface. The
    production seam (``create_run`` / ``guardrail_override``) uses
    :func:`run_interception_pass_async`, which adds the per-guardrail hard
    timeout + bounded-payload budget.
    """
    if not definitions:
        return GuardrailInterceptionOutcome(payload=dict(payload), results=[])
    pre_act = copy.deepcopy(payload)
    results = evaluate_guardrails(engine, definitions, pre_act, raise_on_block=False)

    blocked, block_message, blocking_eval_name = _detect_block(definitions, results)
    if detection_only:
        return GuardrailInterceptionOutcome(payload=pre_act, results=results, blocked=False)

    redacted, entries, rb, rbm, rbname = _apply_redaction_phase(definitions, pre_act)
    if not blocked and rb:
        blocked, block_message, blocking_eval_name = rb, rbm, rbname
    return GuardrailInterceptionOutcome(
        payload=redacted,
        results=results,
        redactions=entries,
        blocked=blocked,
        block_message=block_message,
        blocking_eval_name=blocking_eval_name,
    )


async def run_interception_pass_async(
    engine: EvalEngine,
    definitions: Sequence[EvalDefinition],
    payload: dict[str, Any],
    *,
    detection_only: bool = False,
    timeout_seconds: float | None = None,
    max_payload_bytes: int = DEFAULT_MAX_GUARDRAIL_PAYLOAD_BYTES,
    skipped: Sequence[GuardrailSkip] = (),
) -> GuardrailInterceptionOutcome:
    """Async two-phase guardrail pass with per-guardrail hard timeouts (item 7).

    Identical semantics to :func:`run_interception_pass` PLUS:

    * **Per-guardrail hard timeout** — each detection runs in a worker thread
      under ``asyncio.wait_for`` (default
      :data:`DEFAULT_GUARDRAIL_TIMEOUT_SECONDS`, configurable per-row). A
      timeout is a mechanism error: fail CLOSED for block/redact guardrails
      (recorded as a block), log-and-continue for observe/warn (recorded as a
      failed result so it stays observable).
    * **Bounded payload** — a payload over ``max_payload_bytes`` (ReDoS
      amplification guard) fails closed for block/redact and log-and-continues
      for observe/warn. NEVER a raw payload in any log line.
    * **Skipped guardrails** — *skipped* entries (e.g. a soft-deleted pinned
      guardrail, item 10) are carried on the outcome; the seam audits + alerts.
    * **Detection-only replays** (``detection_only=True``, item 10) never act,
      but a mechanism error is ALWAYS recorded as an errored result — even for
      block/redact guardrails. The replay must preserve the evidence of what
      happened (including errors) for the guardrail_summary errored bucket;
      it is never silently dropped.

    A detection that raises (malformed config, misrouting) is handled the same
    way as a timeout: fail-closed for block/redact, log-and-continue for
    observe/warn — except in detection-only mode, where a guarding guardrail's
    mechanism error is recorded (not dropped) so the replay keeps the evidence.
    """
    if not definitions:
        return GuardrailInterceptionOutcome(payload=dict(payload), skipped=list(skipped))

    if not check_payload_within_budget(payload, max_payload_bytes):
        any_guarding = any(_resolve_action(d) in (GuardrailAction.BLOCK, GuardrailAction.REDACT) for d in definitions)
        reason = f"payload exceeds {max_payload_bytes}-byte guardrail budget"
        if detection_only:
            # Detection-only replays never act (item 10): the over-budget
            # mechanism error is ALWAYS recorded as an errored result for every
            # bound guardrail — never a block — so the replay keeps the evidence
            # (guardrail_summary errored bucket). Mirrors the in-loop
            # detection-only mechanism-error handling below.
            _log.warning("guardrails.payload_over_budget", extra={"reason": reason})
            return GuardrailInterceptionOutcome(
                payload=copy.deepcopy(payload),
                results=[_mechanism_fail_result(d, reason) for d in definitions],
                skipped=list(skipped),
            )
        if any_guarding:
            _log.warning("guardrails.payload_over_budget", extra={"reason": reason})
            return GuardrailInterceptionOutcome(
                payload=copy.deepcopy(payload),
                skipped=list(skipped),
                blocked=True,
                block_message="guardrail mechanism error at ingestion edge",
                blocking_eval_name="<payload-budget>",
            )
        _log.warning("guardrails.payload_over_budget_observe", extra={"reason": reason})
        return GuardrailInterceptionOutcome(
            payload=copy.deepcopy(payload),
            results=[_mechanism_fail_result(d, reason) for d in definitions],
            skipped=list(skipped),
        )

    timeout = timeout_seconds if timeout_seconds is not None else resolve_guardrail_timeout(definitions)
    pre_act = copy.deepcopy(payload)
    results: list[EvalResult] = []
    evaluated_defs: list[EvalDefinition] = []
    blocked: bool = False
    block_message: str = ""
    blocking_eval_name: str = ""
    for eval_def in definitions:
        action = _resolve_action(eval_def)
        guarding = action in (GuardrailAction.BLOCK, GuardrailAction.REDACT)
        try:
            # NOTE — known trade-off (runaway thread): ``wait_for`` only bounds
            # how long WE wait; the worker thread started by ``to_thread`` keeps
            # running in the pool even after the budget fires (a pathological
            # regex can keep spinning in the background). Under repeated
            # pathological patterns this can exhaust the default thread pool
            # executor (one thread per guardrail per run). The 1MB payload
            # budget bounds but does not eliminate it; a future fix could run
            # detection in a cancellable process/sandbox instead.
            result = await asyncio.wait_for(
                asyncio.to_thread(_detect_one, engine, pre_act, eval_def),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Mechanism error (timeout, malformed config, misrouting). Fail
            # closed for block/redact; log-and-continue for observe/warn.
            # In detection_only mode the mechanism error is ALWAYS recorded as
            # an errored result — never dropped — so the replay keeps the
            # evidence of what happened (guardrail_summary errored bucket).
            reason = (
                f"detection exceeded {timeout:g}s budget"
                if isinstance(exc, TimeoutError)
                else f"detection error: {exc}"
            )
            _log.warning(
                "guardrails.detection_budget_exceeded",
                extra={"guardrail": eval_def.name, "reason": reason},
            )
            if guarding and not detection_only:
                blocked = True
                block_message = "guardrail mechanism error at ingestion edge"
                blocking_eval_name = eval_def.name
                continue
            results.append(_mechanism_fail_result(eval_def, reason))
            evaluated_defs.append(eval_def)
            continue
        results.append(result)
        evaluated_defs.append(eval_def)

    if not blocked:
        blocked, block_message, blocking_eval_name = _detect_block(evaluated_defs, results)
    if detection_only:
        return GuardrailInterceptionOutcome(
            payload=pre_act,
            results=results,
            skipped=list(skipped),
            blocked=False,
        )

    redacted, entries, rb, rbm, rbname = _apply_redaction_phase(definitions, pre_act)
    if not blocked and rb:
        blocked, block_message, blocking_eval_name = rb, rbm, rbname
    return GuardrailInterceptionOutcome(
        payload=redacted,
        results=results,
        redactions=entries,
        blocked=blocked,
        block_message=block_message,
        blocking_eval_name=blocking_eval_name,
        skipped=list(skipped),
    )


# ---------------------------------------------------------------------------
# Conformance enforcement (item 7 "Plus") — dispatch-time wiring
# ---------------------------------------------------------------------------


def non_conformant_blocking_guardrails(
    definitions: Sequence[EvalDefinition],
    registered: dict[str, bool | None],
) -> list[tuple[EvalDefinition, ConformanceDerivation]]:
    """Block-action guardrails whose conformance claim is NOT satisfied.

    *registered* maps a capability to True (confirmed present) / False
    (confirmed absent) / None (unreadable). Fail-closed: a block-action
    guardrail with ``required_capabilities`` whose derivation is ``absent`` OR
    ``unknown`` is non-conformant. observe/warn guardrails never participate.
    """
    out: list[tuple[EvalDefinition, ConformanceDerivation]] = []
    for eval_def in definitions:
        if _resolve_action(eval_def) != GuardrailAction.BLOCK:
            continue
        required = eval_def.config.get("required_capabilities") or []
        if not required:
            continue
        derivation = derive_conformance_state([str(c) for c in required], registered)
        if derivation.state in ("absent", "unknown"):
            out.append((eval_def, derivation))
    return out


# ---------------------------------------------------------------------------
# Skip auditing + enforcement-gap alerts (item 10)
# ---------------------------------------------------------------------------


async def notify_guardrail_event(
    org_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: uuid.UUID | None = None,
) -> None:
    """Best-effort guardrail alert via the shared notifier (fail-open WITH a log).

    Lazy-imports the notifier so this module stays importable without an app
    engine. A dispatch failure is logged and swallowed — the enforcement
    (block/skip) already happened; the alert is observability.
    """
    try:
        from modulo.core.notifier import Notifier
        from modulo.db.session import get_shared_engine
        from modulo.settings import get_settings

        settings = get_settings()
        notifier = Notifier(get_shared_engine(), settings.fernet_key)
        await notifier.dispatch_event(org_id, event_type, payload, run_id=run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "guardrails.alert_dispatch_failed",
            extra={"org_id": str(org_id), "event_type": event_type},
        )


async def audit_guardrail_skip(
    session: Any,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    skip: GuardrailSkip,
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Audit a skipped guardrail and raise an enforcement-gap alert (item 10).

    A guardrail referenced by a snapshot pin whose live row no longer exists
    is SKIPPED — never a run failure. The skip is recorded as a
    ``guardrail.skipped`` audit event AND an enforcement-gap notification so
    the operator sees the control has silently stopped enforcing. Both are
    best-effort (fail-open WITH a log) — the skip itself is the policy.
    """
    try:
        from modulo.core.audit_logger import append_audit_event

        await append_audit_event(
            session,
            org_id=org_id,
            event_type="guardrail.skipped",
            actor_user_id=actor_id,
            resource_type="run",
            resource_id=run_id,
            payload_json={"guardrail": skip.name, "reason": skip.reason, "detail": skip.detail},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "guardrails.skip_audit_failed",
            extra={"org_id": str(org_id), "run_id": str(run_id), "guardrail": skip.name},
        )
    await notify_guardrail_event(
        org_id,
        "guardrail_enforcement_gap",
        {"guardrail": skip.name, "reason": skip.reason, "detail": skip.detail, "run_id": str(run_id)},
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Item 11 — guardrail_summary telemetry (run detail)
# ---------------------------------------------------------------------------

# A skip is EXPECTED when its reason is explained by snapshot-pin state — a
# pinned guardrail whose live row is soft-deleted is skipped BY DESIGN (item
# 10) and is never "unexpected". Any skip reason OUTSIDE this set is
# unexpected: the guardrail was skipped for a reason not explained by pin
# state, and must page an alert so the operator sees a control silently
# stopped evaluating.
GUARDRAIL_SKIP_EXPECTED_REASONS: frozenset[str] = frozenset({"soft_deleted"})

# Detail marker written by ``_mechanism_fail_result`` (timeout / over-budget /
# malformed config). The summary derivation recognises an errored row by this
# marker; it is a stable substring of the persisted detail (no raw payload).
GUARDRAIL_MECHANISM_ERROR_MARKER = "mechanism error:"


@dataclass(frozen=True)
class GuardrailSummary:
    """Per-run guardrail interception snapshot (item 11).

    ``bound`` is the number of guardrail rows bound to the pipeline (or pinned
    set) at run start, INCLUDING skipped pins. The invariant
    ``evaluated + errored + skipped == bound`` holds by construction:
    ``errored`` absorbs every bound guardrail that did not produce a clean
    detection (mechanism-error result rows, fail-closed blocked-mechanism
    errors, pre-pass blocks such as a cap violation or conformance block).

    ``passed`` / ``violated`` follow the GUARDRAIL detection semantics (NOT the
    raw ``passed`` column): for a regex guardrail ``passed=True`` means the
    pattern MATCHED — which is a violation. ``passed`` counts clean detections
    that found NO violation; ``violated`` counts clean detections that fired.
    """

    bound: int
    evaluated: int
    passed: int
    violated: int
    observed: int
    errored: int
    redacted: int
    skipped: int
    expected_skips: int

    @property
    def unexpected_skips(self) -> int:
        """Skips not explained by soft-deleted pin state (alert-worthy)."""
        return self.skipped - self.expected_skips

    def to_dict(self) -> dict[str, int]:
        return {
            "bound": self.bound,
            "evaluated": self.evaluated,
            "passed": self.passed,
            "violated": self.violated,
            "observed": self.observed,
            "errored": self.errored,
            "redacted": self.redacted,
            "skipped": self.skipped,
            "expected_skips": self.expected_skips,
            "unexpected_skips": self.unexpected_skips,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> GuardrailSummary:
        """Coerce a persisted summary dict back to a dataclass.

        Raises ValueError on a malformed shape — callers degrade to None.
        """
        if not isinstance(data, Mapping):
            raise ValueError("guardrail summary must be a mapping")
        try:
            return cls(
                bound=int(data["bound"]),
                evaluated=int(data["evaluated"]),
                passed=int(data["passed"]),
                violated=int(data["violated"]),
                observed=int(data["observed"]),
                errored=int(data["errored"]),
                redacted=int(data["redacted"]),
                skipped=int(data["skipped"]),
                expected_skips=int(data["expected_skips"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed guardrail summary: {exc}") from exc


def _is_mechanism_error_result(result: EvalResult) -> bool:
    """True when *result* is a synthetic mechanism-error row (item 7/11)."""
    return bool(result.detail) and GUARDRAIL_MECHANISM_ERROR_MARKER in result.detail


def _result_is_violation(result: EvalResult, detection_types: Mapping[uuid.UUID, str]) -> bool:
    """Interpret a persisted detection result as a guardrail violation.

    Mirrors :func:`_interpret_violation` using the per-eval detection type so
    the summary's passed/violated buckets match the engine semantics (regex
    ``passed=True`` = pattern matched = violation; json_schema ``passed=False``
    = validation failed = violation). Unknown eval ids default to regex.
    """
    detection_type = detection_types.get(result.eval_id, str(EvalType.REGEX))
    return _interpret_violation(detection_type, result)


def build_guardrail_summary(
    *,
    bound: int,
    definitions: Sequence[EvalDefinition],
    results: Sequence[EvalResult],
    redactions: Sequence[RedactionEntry] = (),
    skipped: Sequence[GuardrailSkip] = (),
    observed_by_eval: Mapping[uuid.UUID, bool] | None = None,
) -> GuardrailSummary:
    """Derive the per-run :class:`GuardrailSummary` snapshot at create time.

    *bound* is ``len(definitions) + len(skipped)`` — every bound guardrail plus
    every skipped pin. Mechanism-error result rows (the
    ``_mechanism_fail_result`` detail marker) are split OUT of ``evaluated``
    into ``errored``; ``errored`` additionally absorbs any bound guardrail that
    produced no clean detection (blocked pre-pass / blocked mechanism error),
    guaranteeing ``evaluated + errored + skipped == bound``.
    """
    detection_types = {d.id: _resolve_detection(d)[0] for d in definitions}
    clean = [r for r in results if not _is_mechanism_error_result(r)]
    evaluated = len(clean)
    violations = [r for r in clean if _result_is_violation(r, detection_types)]
    observed = sum(1 for r in clean if observed_by_eval and observed_by_eval.get(r.eval_id, False))
    skipped_count = len(skipped)
    errored = max(0, bound - evaluated - skipped_count)
    redacted = sum(1 for e in redactions if e.applied)
    expected_skips = sum(1 for s in skipped if s.reason in GUARDRAIL_SKIP_EXPECTED_REASONS)
    return GuardrailSummary(
        bound=bound,
        evaluated=evaluated,
        passed=evaluated - len(violations),
        violated=len(violations),
        observed=observed,
        errored=errored,
        redacted=redacted,
        skipped=skipped_count,
        expected_skips=expected_skips,
    )


def guardrail_pattern_hash(definitions: Sequence[EvalDefinition], eval_id: uuid.UUID) -> str:
    """Short deterministic hash of a guardrail's detection declaration.

    Identifies a pattern in logs without embedding the (potentially long)
    author-supplied regex/schema — the per-pattern fired-signature regression
    key (item 11, 4c).
    """
    for eval_def in definitions:
        if eval_def.id != eval_id:
            continue
        _, effective_config = _resolve_detection(eval_def)
        try:
            payload = json.dumps(effective_config, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = str(eval_id)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return ""


def log_guardrail_fired_signatures(
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    definitions: Sequence[EvalDefinition],
    results: Sequence[EvalResult],
) -> None:
    """Emit one ``guardrails.fired_signature`` log line per clean detection.

    Per-pattern fired-signature regression (item 11, 4c): how often each
    guardrail pattern fired (violated) per run/org, keyed by a deterministic
    ``pattern_hash``. Minimal + deterministic — a structured log, never a UI.
    Mechanism-error rows are excluded (they carry no detection signature).
    """
    detection_types = {d.id: _resolve_detection(d)[0] for d in definitions}
    names = {d.id: d.name for d in definitions}
    for result in results:
        if _is_mechanism_error_result(result):
            continue
        _log.info(
            "guardrails.fired_signature",
            extra={
                "org_id": str(org_id),
                "run_id": str(run_id),
                "guardrail": names.get(result.eval_id, ""),
                "pattern_hash": guardrail_pattern_hash(definitions, result.eval_id),
                "fired": _result_is_violation(result, detection_types),
            },
        )


async def alert_unexpected_guardrail_skip(
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    skip: GuardrailSkip,
) -> None:
    """Page a ``guardrail_unexpected_skip`` alert (Notification Log + Error
    Forwarders) for a skip NOT explained by soft-deleted pin state (item 11,
    4b). Best-effort fail-open via :func:`notify_guardrail_event` — never breaks
    the run."""
    from modulo.core.notifier import EVENT_GUARDRAIL_UNEXPECTED_SKIP

    await notify_guardrail_event(
        org_id,
        EVENT_GUARDRAIL_UNEXPECTED_SKIP,
        {"guardrail": skip.name, "reason": skip.reason, "detail": skip.detail, "run_id": str(run_id)},
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# DB row → engine DTO (live rows + snapshot pins)
# ---------------------------------------------------------------------------


def serialize_guardrail_pin(db_row: Any) -> dict[str, Any]:
    """Serialize a DB ``eval_definitions`` guardrail row into a snapshot pin.

    Item 10: the pipeline snapshot pins the guardrail set so a replay evaluates
    the ORIGINAL conditions, not the live rows. The pin is self-contained
    (carries org/pipeline ids) so it can be rebuilt without the live row.
    """
    return {
        "id": str(db_row.id),
        "org_id": str(db_row.organisation_id),
        "pipeline_id": str(db_row.pipeline_id),
        "node_id": str(db_row.node_id) if db_row.node_id else None,
        "name": db_row.name,
        "eval_type": db_row.eval_type,
        "config_json": dict(db_row.config_json or {}),
        "failure_behaviour": db_row.failure_behaviour,
        "pass_threshold": str(db_row.pass_threshold) if db_row.pass_threshold is not None else None,
        "suite_id": db_row.suite_id,
    }


def fingerprint_guardrail_pins(pins: Sequence[Mapping[str, Any]] | None) -> str | None:
    """Deterministic fingerprint of a serialized guardrail pin set (FAR-309 PR B).

    Computed over the canonical JSON of the pin dicts (sorted so
    re-serialization order never changes the digest). A snapshot's
    ``guardrail_pins_json`` is fingerprinted at snapshot creation; the
    run-start replay seam re-computes the fingerprint of the LOADED pins and
    compares — a mismatch means the pins were tampered with (or drifted) since
    creation and the replay fails closed as a mechanism error. Returns None
    for an empty/absent set (nothing to verify) or a set with no usable dict
    entries.
    """
    if not pins:
        return None
    items = sorted(
        json.dumps(pin, sort_keys=True, separators=(",", ":"), default=str) for pin in pins if isinstance(pin, Mapping)
    )
    if not items:
        return None
    canonical = "[" + ",".join(items) + "]"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def to_engine_definition_from_pin(entry: dict[str, Any]) -> EvalDefinition:
    """Build an engine ``EvalDefinition`` DTO from a snapshot pin entry.

    The pin carries the ORIGINAL (snapshot-time) config — a replay must not
    silently pick up live-row edits to the same guardrail.
    """
    config: dict[str, Any] = entry.get("config_json") or {}
    if not isinstance(config, dict):
        config = {}
    return EvalDefinition(
        id=uuid.UUID(str(entry["id"])),
        org_id=uuid.UUID(str(entry["org_id"])),
        pipeline_id=uuid.UUID(str(entry["pipeline_id"])) if entry.get("pipeline_id") else None,
        node_id=str(entry["node_id"]) if entry.get("node_id") else None,
        name=str(entry["name"]),
        eval_type=EvalType(str(entry["eval_type"])),
        config=config,
        failure_behaviour=str(entry.get("failure_behaviour") or "warn"),
        pass_threshold=float(entry["pass_threshold"]) if entry.get("pass_threshold") else None,
        suite_id=entry.get("suite_id"),
    )


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
    "DEFAULT_GUARDRAIL_TIMEOUT_SECONDS",
    "DEFAULT_MAX_GUARDRAILS_PER_NODE",
    "DEFAULT_MAX_GUARDRAIL_PAYLOAD_BYTES",
    "GUARDRAIL_DETECTION_TYPES",
    "GUARDRAIL_FORBIDDEN_FAILURE_BEHAVIOURS",
    "GUARDRAIL_NEVER_TOUCH_FIELDS",
    "GUARDRAIL_SKIP_EXPECTED_REASONS",
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
    "GuardrailSkip",
    "GuardrailSummary",
    "RedactionEntry",
    "alert_unexpected_guardrail_skip",
    "apply_redaction_masks",
    "audit_guardrail_skip",
    "build_guardrail_summary",
    "check_payload_within_budget",
    "derive_conformance_state",
    "evaluate_guardrails",
    "fingerprint_guardrail_pins",
    "guardrail_cap_violation",
    "guardrail_pattern_hash",
    "log_guardrail_fired_signatures",
    "non_conformant_blocking_guardrails",
    "notify_guardrail_event",
    "resolve_guardrail_cap",
    "resolve_guardrail_timeout",
    "resolve_static_path",
    "run_guardrail_pass",
    "run_interception_pass",
    "run_interception_pass_async",
    "serialize_guardrail_pin",
    "set_static_path",
    "to_engine_definition",
    "to_engine_definition_from_pin",
]
