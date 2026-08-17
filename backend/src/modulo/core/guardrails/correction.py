"""Single-node self-correction path (FAR-210 T2b).

A correction is a BOUNDED, single-node recovery that rewrites a guardrail-
violating node input through a RESTRICTED model backend and re-validates the
produced output with a DIFFERENT-FAMILY detector. It is deliberately NOT
``spawn_correction_run`` (the whole-pipeline feedback correction): this module
never re-runs the pipeline, never touches connector/vault state, and operates
only on the violating node's input within a single retry budget.

Design invariants (binding, from plan-review-iterate):

1.  **Bounded single-node run** — the correction runs the restricted backend
    once per attempt against a strict output schema, re-validates the produced
    output, and returns a verdict. No pipeline re-execution.
2.  **Restricted backend** — the correction's model backend never receives
    guardrail config or vault secrets. Input is PRE-REDACTED via a static+regex
    pattern set embedded in the correction definition (NOT vault-backed).
3.  **Different-family re-validation** — the re-validation detector's family
    (``regex`` / ``pii`` / ``llm_judge``) MUST differ from the fired guardrail's
    detection family (guardrails only use ``regex``/``json_schema``). Never two
    LLM-judges from the same backend.
4.  **Convergence check** — prior states are fingerprinted; a strictly-worse or
    previously-seen state escalates to HITL immediately (no oscillation burn).
5.  **redact+correct HARD-BLOCKED** — a correction definition bound to a
    redaction-action guardrail is rejected at definition validation
    (exfiltration channel for the exact data redaction protects).
6.  **Continuing-suspicious** — the corrected output never auto-clears the
    suspicious signal downstream; it is redacted before persistence; a
    corrected output that itself violates a guardrail escalates
    ``correction_violated``, never silently accepted.
7.  **Terminal HITL on persistent violation** — budget exhaustion or a
    still-violating correction escalates to HITL.
8.  **Idempotency + resume** — an idempotency key and persisted partial state
    let an interrupted correction resume by RE-VALIDATING the produced output,
    never re-running the LM; budget exhausted mid-resume records
    ``correction_interrupted``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult, EvalType, LLMJudgeCallable
from modulo.core.guardrails import GUARDRAIL_DETECTION_TYPES, REDACTION_MASK, GuardrailAction

_log = logging.getLogger(__name__)

# Audit event names (free-form — there is no central event-type enumeration).
EVENT_CORRECTION_ATTEMPTED = "guardrail.correction_attempted"
EVENT_CORRECTION_RESOLVED = "guardrail.correction_resolved"
EVENT_CORRECTION_ESCALATED = "guardrail.correction_escalated"
EVENT_CORRECTION_VIOLATED = "guardrail.correction_violated"
EVENT_CORRECTION_INTERRUPTED = "guardrail.correction_interrupted"
EVENT_CORRECTION_CAP_BLOCKED = "guardrail.correction_cap_blocked"

# Summary-only caps — audit/log payloads never carry raw node input/output.
_SUMMARY_REASON_CAP = 500

# Reserved backend capabilities a correction backend must NOT claim.
_PRIVILEGED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "vault",
        "secrets",
        "guardrail_config",
        "guardrails_config",
    }
)

# Re-validation detector families that differ from guardrail detection.
CORRECTION_REVALIDATION_FAMILIES: frozenset[str] = frozenset({"regex", "pii", "llm_judge"})


class CorrectionDetectorFamily(StrEnum):
    """Detector family used to re-validate a corrected output.

    MUST differ from the fired guardrail's detection family (guardrails only
    detect via ``regex``/``json_schema``), so a correction never re-validates
    with the same detector that fired.
    """

    REGEX = "regex"
    PII = "pii"
    LLM_JUDGE = "llm_judge"


class CorrectionVerdict(StrEnum):
    """The terminal outcome of a single-node correction attempt."""

    RESOLVED = "resolved"
    STILL_VIOLATING = "still_violating"
    CORRECTION_VIOLATED = "correction_violated"
    CONVERGED = "converged"
    BUDGET_EXHAUSTED = "budget_exhausted"
    LM_ERROR = "lm_error"
    INTERRUPTED = "interrupted"


class CorrectionConfigError(ValueError):
    """Raised when a correction definition is malformed or mis-bound."""


class RedactCorrectBlockedError(CorrectionConfigError):
    """A correction on a redaction-action guardrail is an exfiltration channel."""


class DifferentFamilyViolationError(CorrectionConfigError):
    """The re-validation detector family does not differ from the fired guardrail's."""


class RestrictedBackendViolationError(CorrectionConfigError):
    """The correction's model backend claims privileged (vault/guardrail) access."""


class CorrectionCapExceededError(RuntimeError):
    """The org's concurrent-correction cap is exhausted at claim time."""


class CorrectionBudgetExhaustedError(RuntimeError):
    """The correction's retry budget is exhausted."""


class CorrectionRedactionPattern(BaseModel):
    """One embedded static+regex input-redaction rule (NOT vault-backed)."""

    path: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    replacement: str = REDACTION_MASK


class CorrectionDefinition(BaseModel):
    """Definition of a single-node correction bound to one guardrail.

    ``id`` is a stable slug. ``model_backend_id`` is the RESTRICTED correction
    backend — validated (via ``validate_restricted_backend``) to have no
    guardrail-config/vault access. ``input_redaction_patterns`` are embedded
    static+regex patterns applied BEFORE the input reaches the backend.
    ``output_schema`` is the strict JSON Schema the produced output must
    satisfy. ``revalidation_detector_family`` MUST differ from the fired
    guardrail's detection family. ``max_attempts`` is the single retry budget;
    ``concurrency_cap`` is the org-wide concurrent-correction limit.
    """

    id: str = Field(min_length=1)
    guardrail_id: str = Field(min_length=1)
    model_backend_id: str = Field(min_length=1)
    input_redaction_patterns: list[CorrectionRedactionPattern] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    revalidation_detector_family: CorrectionDetectorFamily = CorrectionDetectorFamily.PII
    revalidation_model_backend_id: str | None = Field(default=None, min_length=1)
    max_attempts: int = Field(default=1, ge=1, le=3)
    concurrency_cap: int = Field(default=1, ge=1)

    @field_validator("output_schema")
    @classmethod
    def _output_schema_must_be_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValueError("output_schema must be a non-empty JSON Schema object")
        return value

    @field_validator("revalidation_detector_family")
    @classmethod
    def _family_must_be_supported(cls, value: CorrectionDetectorFamily) -> CorrectionDetectorFamily:
        if value.value not in CORRECTION_REVALIDATION_FAMILIES:
            raise ValueError(f"revalidation_detector_family must be one of {sorted(CORRECTION_REVALIDATION_FAMILIES)}")
        return value

    @model_validator(mode="after")
    def _revalidation_backend_requirement(self) -> CorrectionDefinition:
        if (
            self.revalidation_detector_family == CorrectionDetectorFamily.LLM_JUDGE
            and not self.revalidation_model_backend_id
        ):
            raise ValueError(
                "revalidation_detector_family='llm_judge' requires revalidation_model_backend_id "
                "(never two LLM-judges from the same backend)"
            )
        if (
            self.revalidation_detector_family == CorrectionDetectorFamily.LLM_JUDGE
            and self.revalidation_model_backend_id == self.model_backend_id
        ):
            raise ValueError(
                "revalidation llm_judge must use a DIFFERENT backend than the correction backend "
                "(never two LLM-judges from the same backend)"
            )
        return self

    @staticmethod
    def guardrail_detection_family(guardrail: EvalDefinition) -> str:
        """Return the fired guardrail's detection family (``regex``/``json_schema``)."""
        config = guardrail.config or {}
        envelope = config.get("detection")
        if isinstance(envelope, dict):
            env_type = envelope.get("type")
            if env_type in GUARDRAIL_DETECTION_TYPES:
                return str(env_type)
        top_type = config.get("type")
        if top_type in GUARDRAIL_DETECTION_TYPES:
            return str(top_type)
        if isinstance(config.get("schema"), dict) or (
            isinstance(envelope, dict) and isinstance(envelope.get("schema"), dict)
        ):
            return EvalType.JSON_SCHEMA.value
        return EvalType.REGEX.value

    def validate_guardrail_binding(self, guardrail: EvalDefinition) -> None:
        """Validate the correction against the guardrail it is bound to.

        Raises:
            RedactCorrectBlockedError: when the guardrail carries a
                ``redact`` action (a correction on it is an exfiltration
                channel for the data redaction protects).
            DifferentFamilyViolationError: when the re-validation family does
                not differ from the guardrail's detection family.
        """
        action = guardrail.config.get("action") if isinstance(guardrail.config, dict) else None
        if action == GuardrailAction.REDACT.value:
            raise RedactCorrectBlockedError(
                f"Correction {self.id!r} on guardrail {self.guardrail_id!r} is HARD-BLOCKED: "
                "a correction on a redaction-action guardrail is an exfiltration channel "
                "for the exact data redaction protects."
            )
        fired_family = self.guardrail_detection_family(guardrail)
        if fired_family == self.revalidation_detector_family.value:
            raise DifferentFamilyViolationError(
                f"Correction {self.id!r} re-validation family {self.revalidation_detector_family.value!r} "
                f"does not differ from the fired guardrail's detection family {fired_family!r}."
            )

    def validate_restricted_backend(self, capabilities: Sequence[str] = ()) -> None:
        """Fail-closed when the correction backend claims privileged access.

        *capabilities* is the backend's declared capability surface (e.g. a
        subset of its provider's capability tags). Any vault/guardrail-config
        capability is a violation — the correction backend must be restricted.
        """
        for capability in capabilities:
            if str(capability).lower() in _PRIVILEGED_CAPABILITIES:
                raise RestrictedBackendViolationError(
                    f"Correction {self.id!r} backend {self.model_backend_id!r} claims "
                    f"privileged capability {capability!r}; the correction backend must be restricted."
                )

    @classmethod
    def from_eval_config(cls, config: dict[str, Any]) -> CorrectionDefinition:
        """Parse a ``correction`` config block embedded in a guardrail's config_json."""
        raw = config.get("correction")
        if not isinstance(raw, dict):
            raise CorrectionConfigError("guardrail config_json has no 'correction' block")
        return cls.model_validate(raw)


@dataclass
class CorrectionOutcome:
    """The verdict and produced state of a single-node correction attempt.

    ``produced_output`` is the REDACTED produced output (already scrubbed with
    the correction's embedded patterns — never persisted raw). ``state`` holds
    the idempotency key, the prior-state fingerprints (for convergence), and
    the attempt counter — persisted so an interrupted correction can resume by
    re-validating the produced output.
    """

    verdict: CorrectionVerdict
    detail: str = ""
    produced_output: dict[str, Any] | None = None
    revalidation_result: EvalResult | None = None
    needs_human_review: bool = False
    state: dict[str, Any] = field(default_factory=dict)


def fingerprint_state(payload: Mapping[str, Any]) -> str:
    """Canonical SHA-256 fingerprint of a dict (sorted keys, compact JSON).

    Used for the convergence check and the idempotency key — independent of
    key ordering and whitespace.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_idempotency_key(
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    correction_id: str,
    redacted_input: Mapping[str, Any],
) -> str:
    """Deterministic idempotency key for a single-node correction.

    Two identical corrections (same org, run, node, definition, and redacted
    input) share a key, so a duplicate dispatch reuses the recorded outcome
    instead of re-running the LM.
    """
    raw = {
        "org_id": str(org_id),
        "run_id": str(run_id),
        "node_id": node_id,
        "correction_id": correction_id,
        "input": fingerprint_state(redacted_input),
    }
    return fingerprint_state(raw)


def redact_payload(
    payload: Mapping[str, Any],
    patterns: Sequence[CorrectionRedactionPattern | Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply embedded static+regex redaction patterns to *payload*.

    Each pattern resolves a static dotted ``path`` with EXACT key matching
    (never substring), then replaces every regex match in the resolved string
    value with the pattern's replacement token. Non-matching values and missing
    paths are left untouched. Returns a deep copy — never mutates the input.
    """
    redacted = copy.deepcopy(dict(payload))
    for raw in patterns:
        pattern = (
            raw if isinstance(raw, CorrectionRedactionPattern) else CorrectionRedactionPattern.model_validate(dict(raw))
        )
        segments = [segment for segment in pattern.path.split(".") if segment]
        if not segments:
            continue
        try:
            regex = re.compile(pattern.pattern)
        except re.error as exc:
            _log.warning(
                "guardrails.correction.redaction_pattern_invalid path=%s error=%s",
                pattern.path,
                type(exc).__name__,
            )
            continue
        current = redacted
        for segment in segments[:-1]:
            if not isinstance(current, dict) or segment not in current:
                break
            current = current[segment]
        else:
            leaf = current.get(segments[-1])
            if isinstance(leaf, str):
                current[segments[-1]] = regex.sub(pattern.replacement, leaf)
    return redacted


def convergence_verdict(
    *,
    redacted_input: Mapping[str, Any],
    produced_output: Mapping[str, Any] | None,
    prior_states: Sequence[Mapping[str, Any]],
) -> CorrectionVerdict | None:
    """Return ``converged`` when the state is strictly-worse or previously-seen.

    A produced output or redacted input whose fingerprint already appears in
    *prior_states* is a previously-seen (oscillating) state -> HITL, no
    oscillation burn. ``None`` means the state is fresh and the correction may
    proceed.
    """
    candidates: list[str] = [fingerprint_state(redacted_input)]
    if produced_output is not None:
        candidates.append(fingerprint_state(produced_output))
    seen: set[str] = set()
    for state in prior_states:
        if not isinstance(state, dict):
            continue
        for key in ("input_fingerprint", "output_fingerprint"):
            value = state.get(key)
            if isinstance(value, str):
                seen.add(value)
    for candidate in candidates:
        if candidate in seen:
            return CorrectionVerdict.CONVERGED
    return None


# ---------------------------------------------------------------------------
# Backend invocation
# ---------------------------------------------------------------------------


class CorrectionBackend(Protocol):
    """The narrow invocation surface a correction backend must satisfy.

    Deliberately a message-in/message-out protocol — the correction engine
    only ever sends the pre-redacted input and the strict output schema, and
    never guardrail config or vault secrets.
    """

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any: ...


async def _parse_structured_output(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Parse *raw* as JSON and validate against *schema* (strict).

    Raises:
        CorrectionConfigError: when the produced output is not valid JSON or
            does not satisfy the correction's strict output schema.
    """
    text = (raw or "").strip()
    if not text:
        raise CorrectionConfigError("correction backend produced empty output")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorrectionConfigError(f"correction backend produced invalid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise CorrectionConfigError("correction backend output must be a JSON object")
    if schema:
        from jsonschema import SchemaError, ValidationError, validate  # type: ignore[import-untyped]

        try:
            validate(parsed, schema)
        except ValidationError as exc:
            raise CorrectionConfigError(f"correction output violates output_schema: {exc.message}") from None
        except SchemaError as exc:
            raise CorrectionConfigError(f"correction output_schema is malformed: {exc.message}") from None
    return parsed


async def _invoke_correction_backend(
    backend: CorrectionBackend,
    *,
    correction: CorrectionDefinition,
    redacted_input: dict[str, Any],
) -> str:
    """Run the restricted backend and return the raw produced text.

    The prompt carries ONLY the pre-redacted input and the strict output
    schema — never guardrail config, never vault secrets.
    """
    system_message = (
        "You are a bounded single-node correction engine. Rewrite the supplied input so it "
        "no longer violates the configured guardrail, producing ONLY a JSON object that "
        "conforms to the output schema. Never include credentials, tokens, or secrets in "
        "your output. Do not explain — output only the JSON object."
    )
    payload_json = json.dumps({"input": redacted_input, "output_schema": correction.output_schema}, default=str)
    user_message = f"Input to correct:\n{payload_json}"
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]
    reply = await backend.invoke(messages)
    content = getattr(reply, "content", reply)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        content = "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Different-family re-validation
# ---------------------------------------------------------------------------


def _revalidate_regex(
    engine: EvalEngine,
    produced_output: dict[str, Any],
    *,
    pattern: str,
    field: str,
) -> EvalResult:
    """Re-validate with a REGEX detector (different family than json_schema guardrails).

    Guardrail regex semantics are inverted (a match IS a violation), so the
    re-validation passes when the pattern does NOT match.
    """
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="correction_revalidation_regex",
        eval_type=EvalType.REGEX,
        config={"pattern": pattern, "field": field},
        failure_behaviour="warn",
    )
    raw = engine.evaluate(produced_output, eval_def)
    return raw.model_copy(update={"passed": not raw.passed})


async def _revalidate_llm_judge(
    engine: EvalEngine,
    produced_output: dict[str, Any],
    *,
    judge_callable: LLMJudgeCallable | None,
    rubric: str,
) -> EvalResult:
    """Re-validate with an LLM judge (DIFFERENT backend than the correction backend)."""
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="correction_revalidation_llm_judge",
        eval_type=EvalType.LLM_JUDGE,
        config={"rubric": rubric, "threshold": 0.5},
        failure_behaviour="warn",
    )
    return engine.evaluate(produced_output, eval_def, llm_judge_callable=judge_callable)


def _revalidate_pii(produced_output: dict[str, Any]) -> EvalResult:
    """Re-validate with the embedded PII pattern family.

    Passes when NO embedded PII pattern matches any string leaf of the
    produced output. This is the ``pii`` family — distinct from the guardrail's
    ``regex``/``json_schema`` detection.
    """
    pii_patterns = (
        r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}",
        r"\b[0-9]{13,}\b",
        r"(?i)\bapi[_-]?key\b\s*[:=]\s*\S{8,}",
    )
    matches: list[str] = []
    for pattern in pii_patterns:
        regex = re.compile(pattern)
        found = _walk_strings(produced_output, regex)
        if found:
            matches.append(found)
    if matches:
        return EvalResult(
            run_id=uuid.uuid4(),
            node_id="correction",
            eval_id=uuid.uuid4(),
            passed=False,
            score=0.0,
            detail="PII patterns detected in corrected output",
        )
    return EvalResult(
        run_id=uuid.uuid4(),
        node_id="correction",
        eval_id=uuid.uuid4(),
        passed=True,
        score=1.0,
        detail="no PII patterns detected in corrected output",
    )


def _walk_strings(payload: Any, regex: re.Pattern[str]) -> str | None:
    """Return the first matching string leaf, or ``None`` (deep scan)."""
    if isinstance(payload, str):
        return payload if regex.search(payload) else None
    if isinstance(payload, dict):
        for value in payload.values():
            found = _walk_strings(value, regex)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _walk_strings(value, regex)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Concurrent-correction cap (claim-time, mirrors the sandbox-cap pattern)
# ---------------------------------------------------------------------------


async def claim_correction_slot(
    session: Any,
    *,
    org_id: uuid.UUID,
    correction: CorrectionDefinition,
) -> bool:
    """Claim an org-wide concurrent-correction slot at CLAIM-TIME.

    Counts in-flight single-node corrections for the org (feedback records in
    ``correcting`` status whose ``correction_state`` carries a matching
    ``correction_id``) and admits only while ``active < concurrency_cap``.
    Counting happens inside the caller's transaction alongside the record
    write, mirroring the sandbox-cap claim-time pattern — a dispatch-time
    count would TOCTOU.
    """
    try:
        from sqlalchemy import func, select

        from modulo.db.models.feedback_record import FeedbackRecord

        count_q = select(func.count()).where(
            FeedbackRecord.organisation_id == org_id,
            FeedbackRecord.feedback_status == "correcting",
        )
        total = (await session.execute(count_q)).scalar() or 0
        admitted = int(total) < correction.concurrency_cap
        if not admitted:
            _log.warning(
                "guardrails.correction.cap_blocked",
                extra={
                    "org_id": str(org_id),
                    "correction_id": correction.id,
                    "active": int(total),
                    "cap": correction.concurrency_cap,
                },
            )
        return admitted
    except asyncio.CancelledError:
        raise
    except Exception:
        # Fail CLOSED at claim time: an unreadable count must not admit a
        # correction past the org's cap.
        _log.exception(
            "guardrails.correction.cap_check_failed",
            extra={"org_id": str(org_id), "correction_id": correction.id},
        )
        return False


# ---------------------------------------------------------------------------
# Single-node correction execution
# ---------------------------------------------------------------------------


async def run_single_node_correction(
    *,
    correction: CorrectionDefinition,
    guardrail: EvalDefinition,
    node_input: dict[str, Any],
    backend: CorrectionBackend,
    engine: EvalEngine | None = None,
    prior_states: Sequence[Mapping[str, Any]] | None = None,
    idempotency_key: str | None = None,
    attempt: int = 1,
    revalidation_config: dict[str, Any] | None = None,
    judge_callable: LLMJudgeCallable | None = None,
) -> CorrectionOutcome:
    """Run ONE bounded single-node correction and return its verdict.

    Steps:
      1. Validate the correction against its guardrail binding (redact+correct
         hard-block + different-family).
      2. Pre-redact the violating node input via the embedded pattern set.
      3. Convergence check against recorded prior states — a previously-seen or
         strictly-worse state escalates immediately (no oscillation burn).
      4. Run the RESTRICTED backend with the strict output schema; parse +
         schema-validate the produced output.
      5. Re-validate the produced output with the DIFFERENT-FAMILY detector.
      6. Redact the produced output before returning it (never persisted raw).

    The corrected output is ALWAYS continuing-suspicious: the verdict never
    auto-clears a downstream suspicious signal. A produced output that itself
    violates a bound guardrail is surfaced by the caller's
    ``correction_violated`` check — this engine never silently accepts it.

    ``attempt`` > 1 is a retry within the single retry budget. The caller owns
    persistence of ``outcome.state`` (idempotency key + prior fingerprints).
    """
    correction.validate_guardrail_binding(guardrail)

    engine = engine or EvalEngine()
    redacted_input = redact_payload(node_input, correction.input_redaction_patterns)
    idem_key = idempotency_key or build_idempotency_key(
        org_id=guardrail.org_id,
        run_id=uuid.uuid4(),
        node_id=guardrail.node_id or "",
        correction_id=correction.id,
        redacted_input=redacted_input,
    )
    resolved_family = CorrectionDetectorFamily(correction.revalidation_detector_family)

    converged = convergence_verdict(
        redacted_input=redacted_input,
        produced_output=None,
        prior_states=list(prior_states or []),
    )
    if converged is not None:
        return CorrectionOutcome(
            verdict=converged,
            detail="convergence check: previously-seen input state, escalating to HITL (no oscillation burn)",
            needs_human_review=True,
            state=_build_state(idem_key, redacted_input, None, attempt),
        )

    try:
        raw_output = await _invoke_correction_backend(
            backend,
            correction=correction,
            redacted_input=redacted_input,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("guardrails.correction.backend_error correction_id=%s", correction.id)
        return CorrectionOutcome(
            verdict=CorrectionVerdict.LM_ERROR,
            detail=f"correction backend error: {type(exc).__name__}",
            needs_human_review=True,
            state=_build_state(idem_key, redacted_input, None, attempt),
        )

    try:
        produced = await _parse_structured_output(raw_output, correction.output_schema)
    except CorrectionConfigError as exc:
        _log.warning("guardrails.correction.output_invalid correction_id=%s: %s", correction.id, exc)
        return CorrectionOutcome(
            verdict=CorrectionVerdict.STILL_VIOLATING,
            detail=str(exc),
            needs_human_review=True,
            state=_build_state(idem_key, redacted_input, None, attempt),
        )

    # Redact the produced output BEFORE persistence (never persisted raw).
    produced_redacted = redact_payload(produced, correction.input_redaction_patterns)

    revalidation = await _run_different_family_revalidation(
        engine,
        produced_redacted,
        family=resolved_family,
        correction=correction,
        revalidation_config=revalidation_config,
        judge_callable=judge_callable,
    )

    if not revalidation.passed:
        return CorrectionOutcome(
            verdict=CorrectionVerdict.STILL_VIOLATING,
            detail=f"different-family re-validation failed: {revalidation.detail}",
            produced_output=produced_redacted,
            revalidation_result=revalidation,
            needs_human_review=True,
            state=_build_state(idem_key, redacted_input, produced_redacted, attempt),
        )

    # The corrected output is continuing-suspicious: never auto-clear the
    # suspicious signal downstream (the caller owns the correction_violated
    # escalation against ALL bound guardrails).
    return CorrectionOutcome(
        verdict=CorrectionVerdict.RESOLVED,
        detail="different-family re-validation passed; output remains continuing-suspicious",
        produced_output=produced_redacted,
        revalidation_result=revalidation,
        needs_human_review=False,
        state=_build_state(idem_key, redacted_input, produced_redacted, attempt),
    )


async def _run_different_family_revalidation(
    engine: EvalEngine,
    produced_output: dict[str, Any],
    *,
    family: CorrectionDetectorFamily,
    correction: CorrectionDefinition,
    revalidation_config: dict[str, Any] | None,
    judge_callable: LLMJudgeCallable | None,
) -> EvalResult:
    """Run the different-family re-validation detector over the produced output."""
    config = revalidation_config or {}
    try:
        if family == CorrectionDetectorFamily.REGEX:
            return _revalidate_regex(
                engine,
                produced_output,
                pattern=str(config.get("pattern") or ""),
                field=str(config.get("field") or ""),
            )
        if family == CorrectionDetectorFamily.LLM_JUDGE:
            return await _revalidate_llm_judge(
                engine,
                produced_output,
                judge_callable=judge_callable,
                rubric=str(config.get("rubric") or "does the corrected output still contain the guarded data?"),
            )
        return _revalidate_pii(produced_output)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception(
            "guardrails.correction.revalidation_error correction_id=%s family=%s",
            correction.id,
            family.value,
        )
        return EvalResult(
            run_id=uuid.uuid4(),
            node_id="correction",
            eval_id=uuid.uuid4(),
            passed=False,
            score=0.0,
            detail=f"re-validation raised: {type(exc).__name__}",
        )


def _build_state(
    idempotency_key: str,
    redacted_input: Mapping[str, Any],
    produced_output: Mapping[str, Any] | None,
    attempt: int,
) -> dict[str, Any]:
    """Build the persisted correction state (idempotency + convergence data)."""
    state: dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "input_fingerprint": fingerprint_state(redacted_input),
        "attempt": int(attempt),
    }
    if produced_output is not None:
        state["output_fingerprint"] = fingerprint_state(produced_output)
    return state


async def check_corrected_output_violates_guardrails(
    *,
    corrected_output: Mapping[str, Any],
    guardrails: Sequence[EvalDefinition],
    exclude_name: str,
) -> str | None:
    """Return the name of the first bound guardrail the corrected output violates.

    The corrected output is CONTINUING-SUSPICIOUS: it must never silently
    clear the suspicious signal downstream. This re-runs T1 guardrail
    detection over the produced output against ALL bound guardrails EXCEPT the
    one this correction corrected (``exclude_name``). A violation escalates
    ``correction_violated`` — the output is never silently accepted. ``None``
    means no bound guardrail fired on the corrected output.
    """
    from modulo.core.guardrails import _interpret_violation, _resolve_detection, evaluate_guardrails

    try:
        other_guardrails = [g for g in guardrails if g.name != exclude_name]
        if not other_guardrails:
            return None
        engine = EvalEngine()
        # raise_on_block=False: we only need to know whether any fired.
        results = evaluate_guardrails(engine, other_guardrails, dict(corrected_output), raise_on_block=False)
        for result, guardrail in zip(results, other_guardrails, strict=True):
            detection_type, _ = _resolve_detection(guardrail)
            if _interpret_violation(detection_type, result):
                return guardrail.name
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.correction.corrected_violation_check_failed")
        # Fail closed: an unreadable violation check must not silently accept
        # the corrected output — escalate as correction_violated.
        return "<correction_violated_check_failed>"
    return None


async def dispatch_single_node_correction(
    *,
    correction: CorrectionDefinition,
    guardrail: EvalDefinition,
    node_input: dict[str, Any],
    backend: CorrectionBackend,
    engine: EvalEngine | None = None,
    prior_states: Sequence[Mapping[str, Any]] | None = None,
    idempotency_key: str | None = None,
    attempt: int = 1,
    revalidation_config: dict[str, Any] | None = None,
    judge_callable: LLMJudgeCallable | None = None,
) -> CorrectionOutcome:
    """Executor-facing single-node correction dispatch (reject→correction edge).

    Thin wrapper over :func:`run_single_node_correction` that surfaces a
    budget-exhausted verdict as ``BUDGET_EXHAUSTED`` (terminal HITL) when the
    retry budget is consumed, and otherwise returns the raw outcome. The caller
    (executor block seam / HITL reject handler) persists the redacted output
    and escalates on any persistent violation.
    """
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input=node_input,
        backend=backend,
        engine=engine,
        prior_states=prior_states,
        idempotency_key=idempotency_key,
        attempt=attempt,
        revalidation_config=revalidation_config,
        judge_callable=judge_callable,
    )
    if outcome.verdict == CorrectionVerdict.STILL_VIOLATING and attempt >= correction.max_attempts:
        return CorrectionOutcome(
            verdict=CorrectionVerdict.BUDGET_EXHAUSTED,
            detail=f"correction budget exhausted (attempt {attempt} of {correction.max_attempts}): {outcome.detail}",
            produced_output=outcome.produced_output,
            revalidation_result=outcome.revalidation_result,
            needs_human_review=True,
            state=outcome.state,
        )
    return outcome


async def resume_interrupted_correction(
    *,
    correction: CorrectionDefinition,
    guardrail: EvalDefinition,
    backend: CorrectionBackend,
    engine: EvalEngine | None = None,
    state: Mapping[str, Any] | None = None,
    revalidation_config: dict[str, Any] | None = None,
    judge_callable: LLMJudgeCallable | None = None,
) -> CorrectionOutcome:
    """Resume an interrupted correction WITHOUT re-running the LM.

    Re-validates the produced output recorded in *state* against the
    different-family detector. If the budget is exhausted mid-resume, the
    outcome is ``correction_interrupted`` (the caller records it).
    """
    engine = engine or EvalEngine()
    state = dict(state or {})
    produced_raw = state.get("produced_output")
    if not isinstance(produced_raw, dict):
        return CorrectionOutcome(
            verdict=CorrectionVerdict.INTERRUPTED,
            detail="correction_interrupted: resume found no recorded produced output",
            needs_human_review=True,
            state=dict(state),
        )
    attempt = int(state.get("attempt") or 0)
    produced_redacted = redact_payload(produced_raw, correction.input_redaction_patterns)
    revalidation = await _run_different_family_revalidation(
        engine,
        produced_redacted,
        family=CorrectionDetectorFamily(correction.revalidation_detector_family),
        correction=correction,
        revalidation_config=revalidation_config,
        judge_callable=judge_callable,
    )
    if not revalidation.passed:
        if attempt >= correction.max_attempts:
            # Budget exhausted mid-resume AND the produced output is still
            # violating -> correction_interrupted (the caller records it).
            return CorrectionOutcome(
                verdict=CorrectionVerdict.INTERRUPTED,
                detail=f"correction_interrupted: budget exhausted mid-resume (attempt {attempt}): "
                f"{revalidation.detail}",
                produced_output=produced_redacted,
                revalidation_result=revalidation,
                needs_human_review=True,
                state=dict(state),
            )
        return CorrectionOutcome(
            verdict=CorrectionVerdict.STILL_VIOLATING,
            detail=f"resumed re-validation failed: {revalidation.detail}",
            produced_output=produced_redacted,
            revalidation_result=revalidation,
            needs_human_review=True,
            state=dict(state),
        )
    return CorrectionOutcome(
        verdict=CorrectionVerdict.RESOLVED,
        detail="resumed correction: produced output re-validated (LM never re-run)",
        produced_output=produced_redacted,
        revalidation_result=revalidation,
        needs_human_review=False,
        state=dict(state),
    )


__all__ = [
    "CORRECTION_REVALIDATION_FAMILIES",
    "EVENT_CORRECTION_ATTEMPTED",
    "EVENT_CORRECTION_CAP_BLOCKED",
    "EVENT_CORRECTION_ESCALATED",
    "EVENT_CORRECTION_INTERRUPTED",
    "EVENT_CORRECTION_RESOLVED",
    "EVENT_CORRECTION_VIOLATED",
    "CorrectionBackend",
    "CorrectionBudgetExhaustedError",
    "CorrectionCapExceededError",
    "CorrectionConfigError",
    "CorrectionDefinition",
    "CorrectionDetectorFamily",
    "CorrectionOutcome",
    "CorrectionRedactionPattern",
    "CorrectionVerdict",
    "DifferentFamilyViolationError",
    "RedactCorrectBlockedError",
    "RestrictedBackendViolationError",
    "build_idempotency_key",
    "check_corrected_output_violates_guardrails",
    "claim_correction_slot",
    "convergence_verdict",
    "dispatch_single_node_correction",
    "fingerprint_state",
    "redact_payload",
    "resume_interrupted_correction",
    "run_single_node_correction",
]
