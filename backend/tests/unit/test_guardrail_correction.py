"""Unit tests for the FAR-210 T2b single-node self-correction path.

Covers the genuinely-new bounded correction engine (``modulo.core.guardrails.correction``):

  * correction definition validation: redact+correct HARD-BLOCK, different-family
    enforcement, restricted-backend validation, llm_judge backend split;
  * embedded static+regex input redaction (not vault-backed);
  * the bounded ``run_single_node_correction`` flow: pre-redaction -> restricted
    backend -> strict output schema -> different-family re-validation -> verdict;
  * convergence check (previously-seen state -> escalate, no oscillation burn);
  * continuing-suspicious semantics (never auto-clears downstream);
  * redaction before persistence of the produced output;
  * idempotency key + persisted partial state + ``resume_interrupted_correction``
    (re-validates the produced output, never re-runs the LM);
  * ``dispatch_single_node_correction`` budget-exhaustion (terminal HITL) mapping;
  * org-wide concurrent-correction cap claim (claim-time).

No DB is required — the session/backend are stubbed. Uses a real
``StubModelBackend`` (LangChain) wrapped in a tiny dict-to-BaseMessage adapter.
"""

import json
import uuid
from typing import Any

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails.correction import (
    CorrectionDefinition,
    CorrectionDetectorFamily,
    CorrectionVerdict,
    DifferentFamilyViolationError,
    RedactCorrectBlockedError,
    RestrictedBackendViolationError,
    build_idempotency_key,
    convergence_verdict,
    dispatch_single_node_correction,
    fingerprint_state,
    redact_payload,
    resume_interrupted_correction,
    run_single_node_correction,
)
from modulo.model_backends.stub.backend import StubModelBackend, normalize_input

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _to_base_message(messages: list[dict[str, Any]]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            out.append(SystemMessage(content=content))
        else:
            out.append(HumanMessage(content=content))
    return out


class _StubCorrectionBackend:
    """Wraps StubModelBackend for the correction engine's dict-message protocol."""

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        del kwargs
        return await self._inner.ainvoke(_to_base_message(messages))

    @property
    def backend_id(self) -> str:
        return "stub"


def _guardrail(
    *,
    action: str = "block",
    detection_type: str = "regex",
    pattern: str = r"(?i)secret[:=]\s*\S+",
    field: str = "body",
    name: str = "gr_no_secrets",
) -> EvalDefinition:
    """Build a fired guardrail EvalDefinition mirroring the T1 guardrail shape."""
    config: dict[str, Any] = {
        "interception_point": "input",
        "action": action,
        "detection": {"type": detection_type, "pattern": pattern, "field": field},
    }
    if detection_type == "json_schema":
        config["detection"] = {"type": "json_schema", "schema": {"type": "object"}}
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG,
        pipeline_id=uuid.uuid4(),
        node_id="node_a",
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour="warn",
    )


def _correction(**overrides: Any) -> CorrectionDefinition:
    base: dict[str, Any] = {
        "id": "corr_no_secrets",
        "guardrail_id": "gr_no_secrets",
        "model_backend_id": str(uuid.uuid4()),
        "input_redaction_patterns": [
            {"path": "body", "pattern": r"(?i)secret[:=]\s*\S+", "replacement": "\u2022\u2022\u2022"},
        ],
        "output_schema": {"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}}},
        "revalidation_detector_family": CorrectionDetectorFamily.PII.value,
        "max_attempts": 1,
        "concurrency_cap": 1,
    }
    base.update(overrides)
    return CorrectionDefinition.model_validate(base)


_SYSTEM_MESSAGE = (
    "You are a bounded single-node correction engine. Rewrite the supplied input so it "
    "no longer violates the configured guardrail, producing ONLY a JSON object that "
    "conforms to the output schema. Never include credentials, tokens, or secrets in "
    "your output. Do not explain — output only the JSON object."
)


def _fixture_key(correction: CorrectionDefinition, redacted_input: dict[str, Any]) -> str:
    """Build the stub-backend fixture key matching the engine's normalized message input."""
    payload_json = json.dumps({"input": redacted_input, "output_schema": correction.output_schema})
    user_message = f"Input to correct:\n{payload_json}"
    messages = [
        SystemMessage(content=_SYSTEM_MESSAGE),
        HumanMessage(content=user_message),
    ]
    return normalize_input(messages)


_REDACTED_BODY = {"body": "\u2022\u2022\u2022"}


# ---------------------------------------------------------------------------
# Correction definition validation
# ---------------------------------------------------------------------------


def test_redact_correct_hard_blocked():
    guardrail = _guardrail(action="redact")
    correction = _correction()
    with pytest.raises(RedactCorrectBlockedError, match="HARD-BLOCKED"):
        correction.validate_guardrail_binding(guardrail)


def test_different_family_enforced():
    """A correction whose re-validation family equals the fired guardrail's is rejected."""
    # Fired guardrail detects via regex; re-validation is ALSO regex -> violation.
    guardrail = _guardrail(action="block", detection_type="regex")
    correction = _correction(
        revalidation_detector_family=CorrectionDetectorFamily.REGEX.value,
        revalidation_config={"pattern": r"(?i)secret[:=]\s*\S+", "field": "body"},
    )
    with pytest.raises(DifferentFamilyViolationError, match="does not differ"):
        correction.validate_guardrail_binding(guardrail)


def test_regex_family_requires_revalidation_pattern_at_definition_validation():
    """MAJOR-1: a REGEX-family correction with no pattern is rejected at definition
    validation — a regex revalidator with no pattern would fail open (marked
    resolved with no actual re-validation)."""
    base = {
        "id": "corr_regex",
        "guardrail_id": "gr",
        "model_backend_id": str(uuid.uuid4()),
        "output_schema": {"type": "object"},
        "revalidation_detector_family": CorrectionDetectorFamily.REGEX.value,
    }
    with pytest.raises(ValueError, match=r"revalidation_config\.pattern"):
        CorrectionDefinition.model_validate(base)
    with pytest.raises(ValueError, match=r"revalidation_config\.pattern"):
        CorrectionDefinition.model_validate({**base, "revalidation_config": {"field": "body"}})
    with pytest.raises(ValueError, match=r"revalidation_config\.field"):
        CorrectionDefinition.model_validate({**base, "revalidation_config": {"pattern": r"(?i)secret[:=]\s*\S+"}})
    ok = CorrectionDefinition.model_validate(
        {**base, "revalidation_config": {"pattern": r"(?i)secret[:=]\s*\S+", "field": "body"}}
    )
    assert ok.revalidation_config["pattern"]


def test_regex_revalidation_with_empty_pattern_fails_closed():
    """MAJOR-1: a regex revalidator with an empty pattern/field must NOT mark the
    correction resolved (fail closed) — the empty-pattern engine failure is not
    inverted into a false pass."""
    from modulo.core.eval_engine import EvalEngine
    from modulo.core.guardrails.correction import _revalidate_regex

    engine = EvalEngine()
    # Empty pattern/field (the pre-fix default) -> passed=False (fail closed).
    result = _revalidate_regex(engine, {"body": "anything"}, pattern="", field="")
    assert result.passed is False
    result = _revalidate_regex(engine, {"body": "anything"}, pattern="secret: x", field="")
    assert result.passed is False
    # A PRESENT pattern that does NOT match -> passes (the guarded value is gone).
    result = _revalidate_regex(engine, {"body": "safe now"}, pattern=r"(?i)secret[:=]\s*\S+", field="body")
    assert result.passed is True
    # A present pattern that DOES match -> fails (the guarded value is still there).
    result = _revalidate_regex(engine, {"body": "secret: hunter2"}, pattern=r"(?i)secret[:=]\s*\S+", field="body")
    assert result.passed is False


def test_restricted_backend_rejects_privileged_capabilities():
    correction = _correction()
    with pytest.raises(RestrictedBackendViolationError, match="restricted"):
        correction.validate_restricted_backend(["vault"])
    with pytest.raises(RestrictedBackendViolationError, match="restricted"):
        correction.validate_restricted_backend(["guardrail_config"])
    correction.validate_restricted_backend(["filesystem", "http"])  # benign


def test_llm_judge_revalidation_requires_different_backend():
    backend_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="revalidation_model_backend_id"):
        CorrectionDefinition.model_validate(
            {
                "id": "c",
                "guardrail_id": "g",
                "model_backend_id": backend_id,
                "output_schema": {"type": "object"},
                "revalidation_detector_family": "llm_judge",
            }
        )
    with pytest.raises(ValueError, match="DIFFERENT backend"):
        CorrectionDefinition.model_validate(
            {
                "id": "c",
                "guardrail_id": "g",
                "model_backend_id": backend_id,
                "output_schema": {"type": "object"},
                "revalidation_detector_family": "llm_judge",
                "revalidation_model_backend_id": backend_id,
            }
        )
    ok = CorrectionDefinition.model_validate(
        {
            "id": "c",
            "guardrail_id": "g",
            "model_backend_id": backend_id,
            "output_schema": {"type": "object"},
            "revalidation_detector_family": "llm_judge",
            "revalidation_model_backend_id": str(uuid.uuid4()),
        }
    )
    assert ok.revalidation_model_backend_id != ok.model_backend_id


def test_from_eval_config_parses_correction_block():
    config = {
        "interception_point": "input",
        "action": "block",
        "correction": {
            "id": "c1",
            "guardrail_id": "g1",
            "model_backend_id": "mb-1",
            "output_schema": {"type": "object"},
        },
    }
    correction = CorrectionDefinition.from_eval_config(config)
    assert correction.id == "c1"
    assert correction.guardrail_id == "g1"


# ---------------------------------------------------------------------------
# Redaction + fingerprints
# ---------------------------------------------------------------------------


def test_redact_payload_applies_embedded_patterns():
    payload = {"body": "the secret: hunter2 is here", "safe": "hello"}
    redacted = redact_payload(
        payload,
        [
            {
                "path": "body",
                "pattern": r"(?i)secret[:=]\s*\S+",
                "replacement": "\u2022\u2022\u2022",
            }
        ],
    )
    assert "hunter2" not in redacted["body"]
    assert redacted["safe"] == "hello"
    # Original is never mutated.
    assert "hunter2" in payload["body"]


def test_redact_payload_exact_path_matching_only():
    payload = {"nested": {"body": "secret: abc"}, "otherbody": "secret: xyz"}
    redacted = redact_payload(
        payload,
        [{"path": "nested.body", "pattern": r"(?i)secret[:=]\s*\S+", "replacement": "***"}],
    )
    assert redacted["nested"]["body"] == "***"
    assert redacted["otherbody"] == "secret: xyz"


def test_fingerprint_state_canonical():
    assert fingerprint_state({"a": 1, "b": 2}) == fingerprint_state({"b": 2, "a": 1})


def test_build_idempotency_key_deterministic():
    kwargs = {
        "org_id": _ORG,
        "run_id": uuid.UUID("00000000-0000-0000-0000-0000000000c1"),
        "node_id": "node_a",
        "correction_id": "corr_no_secrets",
        "redacted_input": {"body": "secret: abc"},
    }
    first = build_idempotency_key(**kwargs)
    second = build_idempotency_key(**kwargs)
    assert first == second


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def test_convergence_detects_previously_seen_input():
    redacted = {"body": "same"}
    prior = [{"input_fingerprint": fingerprint_state(redacted)}]
    assert convergence_verdict(redacted_input=redacted, produced_output=None, prior_states=prior) == (
        CorrectionVerdict.CONVERGED
    )


def test_convergence_allows_fresh_state():
    assert (
        convergence_verdict(
            redacted_input={"body": "fresh"},
            produced_output=None,
            prior_states=[{"input_fingerprint": fingerprint_state({"body": "old"})}],
        )
        is None
    )


def test_convergence_detects_strictly_worse_state():
    """FAR-292 prove-the-fix: a current state whose violation severity strictly
    exceeds every recorded prior state's severity escalates to HITL immediately
    (CONVERGED). Without the strictly-worse ordering this returns ``None``
    (the fingerprint is fresh)."""
    prior = [{"violation_metric": {"severity": 1, "detail_fingerprint": "prior"}}]
    assert (
        convergence_verdict(
            redacted_input={"body": "new input"},
            produced_output={"body": "new output"},
            prior_states=prior,
            current_violation_metric={"severity": 2, "detail_fingerprint": "current"},
        )
        == CorrectionVerdict.CONVERGED
    )


def test_convergence_allows_equal_or_better_state():
    """FAR-292: an equal-or-better violation severity allows a fresh attempt
    (None), even when the violation detail differs — a still-violating retry
    with a different (but equally severe) violation keeps its budget."""
    prior = [{"output_violation_metric": {"severity": 1, "detail_fingerprint": "prior"}}]
    assert (
        convergence_verdict(
            redacted_input={"body": "new input"},
            produced_output={"body": "new output"},
            prior_states=prior,
            current_violation_metric={"severity": 1, "detail_fingerprint": "different"},
        )
        is None
    )
    # A cleaner (lower-severity) current state is also allowed.
    assert (
        convergence_verdict(
            redacted_input={"body": "new input"},
            produced_output={"body": "clean"},
            prior_states=prior,
            current_violation_metric={"severity": 0, "detail_fingerprint": ""},
        )
        is None
    )


def test_convergence_reads_persisted_split_metric_keys():
    """The convergence check compares against BOTH the persisted
    ``input_violation_metric`` and ``output_violation_metric`` keys that
    ``_build_state`` writes (retry loop strips the input metric, preserves the
    output metric)."""
    prior = [{"output_violation_metric": {"severity": 1, "detail_fingerprint": "prior"}}]
    assert (
        convergence_verdict(
            redacted_input={"body": "new input"},
            produced_output={"body": "new output"},
            prior_states=prior,
            current_violation_metric={"severity": 2, "detail_fingerprint": "current"},
        )
        == CorrectionVerdict.CONVERGED
    )


# ---------------------------------------------------------------------------
# Single-node correction execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_correction_resolves_with_redacted_output():
    guardrail = _guardrail()
    correction = _correction()
    backend = _StubCorrectionBackend({_fixture_key(correction, _REDACTED_BODY): json.dumps({"body": "safe now"})})
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
    )
    assert outcome.verdict == CorrectionVerdict.RESOLVED
    assert outcome.produced_output == {"body": "safe now"}
    assert outcome.needs_human_review is False
    # Continuing-suspicious: the verdict never auto-clears a downstream signal.
    assert outcome.detail


@pytest.mark.asyncio
async def test_still_violating_when_revalidation_fails():
    guardrail = _guardrail()
    correction = _correction()
    # The PII re-validation family flags a long digit run -> still violating.
    backend = _StubCorrectionBackend(
        {_fixture_key(correction, _REDACTED_BODY): json.dumps({"body": "123456789012345678901"})}
    )
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
    )
    assert outcome.verdict == CorrectionVerdict.STILL_VIOLATING
    assert outcome.needs_human_review is True


@pytest.mark.asyncio
async def test_lm_error_is_fail_mode():
    class _Raises:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    guardrail = _guardrail()
    correction = _correction()
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=_Raises(),
    )
    assert outcome.verdict == CorrectionVerdict.LM_ERROR
    assert outcome.needs_human_review is True


@pytest.mark.asyncio
async def test_schema_invalid_output_still_violating():
    guardrail = _guardrail()
    correction = _correction()
    backend = _StubCorrectionBackend({_fixture_key(correction, _REDACTED_BODY): "not json at all"})
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
    )
    assert outcome.verdict == CorrectionVerdict.STILL_VIOLATING
    assert outcome.needs_human_review is True


@pytest.mark.asyncio
async def test_corrected_output_redacted_before_returned():
    guardrail = _guardrail()
    correction = _correction()
    # The backend echoes the (already redacted) input plus a new secret value.
    backend = _StubCorrectionBackend(
        {_fixture_key(correction, _REDACTED_BODY): json.dumps({"body": "note secret: hunter2 again"})}
    )
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
    )
    assert outcome.verdict == CorrectionVerdict.RESOLVED
    # The produced output's embedded secret is redacted before it is returned.
    assert "hunter2" not in json.dumps(outcome.produced_output)


@pytest.mark.asyncio
async def test_state_persists_violation_metric():
    """FAR-292: the engine's persisted ``outcome.state`` carries the per-state
    violation metric (input + output) so the next attempt's convergence check
    can compare severity against the recorded prior states."""
    guardrail = _guardrail()
    correction = _correction()
    # The correction's own PII re-validation passes ("safe now"), so the
    # produced output is RESOLVED but the state must still record its metrics.
    backend = _StubCorrectionBackend({_fixture_key(correction, _REDACTED_BODY): json.dumps({"body": "safe now"})})
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
    )
    assert outcome.verdict == CorrectionVerdict.RESOLVED
    assert "input_violation_metric" in outcome.state
    assert "output_violation_metric" in outcome.state
    assert isinstance(outcome.state["input_violation_metric"].get("severity"), int)
    assert isinstance(outcome.state["output_violation_metric"].get("severity"), int)


@pytest.mark.asyncio
async def test_strictly_worse_produced_output_converges_without_burning_budget():
    """FAR-292 prove-the-fix through the real engine: a produced output that
    violates MORE bound guardrails than a recorded prior state (strictly worse)
    converges to HITL immediately — the LM runs once to produce it, then the
    output converges WITHOUT burning the remaining retry budget."""
    guardrail = _guardrail(name="gr_no_secrets")
    gr_digits = _guardrail(name="gr_no_digits", pattern=r"\b[0-9]{10,}\b", field="body")
    gr_admin = _guardrail(name="gr_no_admin", pattern=r"(?i)\badmin\b", field="body")
    correction = _correction(max_attempts=3)
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)

    # Prior state: the recorded produced output violated ONE bound guardrail
    # (severity 1). Its input fingerprint does NOT match the current redacted
    # input, so the input check passes and the output convergence check runs.
    prior = [
        {
            "idempotency_key": "key-1",
            "attempt": 1,
            "input_fingerprint": fingerprint_state({"body": "some other prior input"}),
            "output_violation_metric": {"severity": 1, "detail_fingerprint": "prior"},
        }
    ]

    # The produced output survives redaction (no `secret:`), so it violates BOTH
    # gr_no_digits (long digit run) and gr_no_admin ("admin") -> severity 2,
    # strictly worse than the prior severity 1.
    backend = _StubCorrectionBackend(
        {_fixture_key(correction, redacted): json.dumps({"body": "admin 123456789012345"})}
    )
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
        prior_states=prior,
        bound_guardrails=[guardrail, gr_digits, gr_admin],
    )
    assert outcome.verdict == CorrectionVerdict.CONVERGED
    assert outcome.needs_human_review is True


@pytest.mark.asyncio
async def test_dispatch_maps_budget_exhaustion_to_terminal():
    guardrail = _guardrail()
    correction = _correction(max_attempts=1)
    backend = _StubCorrectionBackend(
        {_fixture_key(correction, _REDACTED_BODY): json.dumps({"body": "123456789012345678901"})}
    )
    outcome = await dispatch_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
        attempt=1,
    )
    assert outcome.verdict == CorrectionVerdict.BUDGET_EXHAUSTED
    assert outcome.needs_human_review is True


# ---------------------------------------------------------------------------
# Idempotency / resume (never re-runs the LM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_revalidates_produced_output_never_reruns_lm():
    guardrail = _guardrail()
    correction = _correction()
    state = {
        "idempotency_key": "key-1",
        "attempt": 1,
        "input_fingerprint": fingerprint_state({"body": "\u2022\u2022\u2022"}),
        "output_fingerprint": fingerprint_state({"body": "safe now"}),
        "produced_output": {"body": "safe now"},
    }

    class _Boom:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT be re-run on resume")

    outcome = await resume_interrupted_correction(
        correction=correction,
        _guardrail=guardrail,
        _backend=_Boom(),
        state=state,
    )
    assert outcome.verdict == CorrectionVerdict.RESOLVED
    assert outcome.produced_output == {"body": "safe now"}


@pytest.mark.asyncio
async def test_resume_budget_exhausted_mid_resume_records_interrupted():
    guardrail = _guardrail()
    correction = _correction(max_attempts=1)
    state = {
        "idempotency_key": "key-1",
        "attempt": 1,
        "produced_output": {"body": "123456789012345678901"},
    }
    outcome = await resume_interrupted_correction(
        correction=correction,
        _guardrail=guardrail,
        _backend=_StubCorrectionBackend({}),
        state=state,
    )
    # attempt >= max_attempts and re-validation still fails -> interrupted.
    assert outcome.verdict == CorrectionVerdict.INTERRUPTED
    assert outcome.needs_human_review is True


@pytest.mark.asyncio
async def test_resume_round_trips_engine_persisted_state():
    """Minor-2 (review FAR-210): the engine's OWN persisted state — the
    ``outcome.state`` a caller would store — carries ``produced_output`` so an
    interrupted correction can resume by re-validating it. Previously
    ``_build_state`` never wrote ``produced_output`` (only
    ``FeedbackManager.run_single_node_correction`` injected it post-hoc), so a
    caller persisting just ``outcome.state`` got ``correction_interrupted``
    ("no recorded produced output") on resume. Without the fix this test FAILS:
    ``outcome.state`` has no ``produced_output`` -> resume returns INTERRUPTED."""
    guardrail = _guardrail()
    correction = _correction()
    backend = _StubCorrectionBackend({_fixture_key(correction, _REDACTED_BODY): json.dumps({"body": "safe now"})})
    outcome = await run_single_node_correction(
        correction=correction,
        guardrail=guardrail,
        node_input={"body": "secret: hunter2"},
        backend=backend,
    )
    assert outcome.verdict == CorrectionVerdict.RESOLVED
    # The engine's own state must carry the redacted produced output.
    assert outcome.state.get("produced_output") == {"body": "safe now"}

    class _Boom:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT be re-run on resume")

    resumed = await resume_interrupted_correction(
        correction=correction,
        _guardrail=guardrail,
        _backend=_Boom(),
        state=outcome.state,
    )
    assert resumed.verdict == CorrectionVerdict.RESOLVED
    assert resumed.produced_output == {"body": "safe now"}


# ---------------------------------------------------------------------------
# Concurrent-correction cap (claim-time)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal session stub returning a configurable active-correction count.

    ``excluding_current_record=True`` models the MAJOR-3 fix: the claim query
    carries an ``id != :excluded`` clause, so the count the DB returns EXCLUDES
    the record currently being corrected. The fake detects that clause and
    drops the current record from the reported count.
    """

    def __init__(self, active_count: int = 0, excluding_current_record: bool = False) -> None:
        self._active = active_count
        self._excluding_current_record = excluding_current_record

    async def execute(self, stmt: Any) -> Any:
        class _Result:
            def scalar(self) -> int:
                return self._count

            def __init__(self, count: int) -> None:
                self._count = count

        count = self._active
        if self._excluding_current_record and "!=" in str(stmt):
            count = max(0, count - 1)
        return _Result(count)


@pytest.mark.asyncio
async def test_claim_slot_respects_concurrency_cap():
    from modulo.core.guardrails.correction import claim_correction_slot

    correction = _correction(concurrency_cap=2)
    assert await claim_correction_slot(_FakeSession(active_count=0), org_id=_ORG, correction=correction) is True
    assert await claim_correction_slot(_FakeSession(active_count=1), org_id=_ORG, correction=correction) is True
    assert await claim_correction_slot(_FakeSession(active_count=2), org_id=_ORG, correction=correction) is False


@pytest.mark.asyncio
async def test_claim_slot_excludes_current_record_self_count():
    """MAJOR-3: the record currently being corrected is NOT counted against the cap.

    cap=1 with one 'correcting' record (the current one) must admit the claim —
    the current record's own 'correcting' status must not block its correction.
    """
    from modulo.core.guardrails.correction import claim_correction_slot

    correction = _correction(concurrency_cap=1)
    assert (
        await claim_correction_slot(
            _FakeSession(active_count=1, excluding_current_record=True),
            org_id=_ORG,
            correction=correction,
            exclude_record_id=uuid.uuid4(),
        )
        is True
    )


@pytest.mark.asyncio
async def test_dispatch_reject_correction_resolves_and_dispatches():
    """The reject→correction seam resolves the guardrail's correction and dispatches.

    Proves the FAR-210 follow-up dispatch reaches the correction path
    (``run_single_node_correction``) with the correction-configured guardrail,
    the embedded CorrectionDefinition, the run, and the blocked node's input.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from modulo.core.feedback_manager import FeedbackManager, dispatch_reject_correction

    run = SimpleNamespace(pipeline_id=uuid.uuid4(), account_id=uuid.uuid4())
    node_input = {"body": "secret: hunter2"}
    record = MagicMock()
    record.id = uuid.uuid4()

    guardrail_config: dict[str, Any] = {
        "interception_point": "input",
        "action": "block",
        "detection": {"type": "regex", "pattern": r"(?i)secret[:=]\s*\S+", "field": "body"},
        "correction": {
            "id": "corr_no_secrets",
            "guardrail_id": "gr_no_secrets",
            "model_backend_id": str(uuid.uuid4()),
            "input_redaction_patterns": [
                {"path": "body", "pattern": r"(?i)secret[:=]\s*\S+", "replacement": "\u2022\u2022\u2022"},
            ],
            "output_schema": {
                "type": "object",
                "required": ["body"],
                "properties": {"body": {"type": "string"}},
            },
            "revalidation_detector_family": CorrectionDetectorFamily.PII.value,
            "max_attempts": 1,
            "concurrency_cap": 1,
        },
    }
    guardrail = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG,
        pipeline_id=run.pipeline_id,
        node_id="node_a",
        name="gr_no_secrets",
        eval_type=EvalType.GUARDRAIL,
        config=guardrail_config,
        failure_behaviour="warn",
    )

    backend = _StubCorrectionBackend({})
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    session_factory = MagicMock(return_value=session)

    hub = MagicMock()
    hub.get = AsyncMock(return_value=backend)

    with (
        patch("modulo.core.feedback_manager.get_run", AsyncMock(return_value=run)),
        patch("modulo.core.guardrails.conformance.load_node_guardrails", AsyncMock(return_value=[guardrail])),
        patch("modulo.core.feedback_manager._get_feedback_record_for_node", AsyncMock(return_value=record)),
        patch("modulo.core.pipeline_engine.decorator.get_model_backend_hub", return_value=hub),
        patch.object(
            FeedbackManager,
            "run_single_node_correction",
            AsyncMock(return_value={"verdict": "resolved"}),
        ) as mock_run,
    ):
        outcome = await dispatch_reject_correction(
            session_factory=session_factory,
            org_id=_ORG,
            run_id=run.pipeline_id,
            node_id="node_a",
            node_input=node_input,
            rejection_reason="secret detected",
            gate_id="hitl_gate_a_b",
        )

    assert outcome == {"verdict": "resolved"}
    mock_run.assert_awaited_once()
    call_kwargs = mock_run.await_args.kwargs
    assert call_kwargs["node_input"] == node_input
    assert call_kwargs["correction"].id == "corr_no_secrets"
    assert call_kwargs["guardrail"].name == "gr_no_secrets"


@pytest.mark.asyncio
async def test_dispatch_reject_correction_no_correction_guardrail_returns_none():
    """A node with no correction-configured guardrail dispatches nothing (best-effort)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from modulo.core.feedback_manager import dispatch_reject_correction

    run = SimpleNamespace(pipeline_id=uuid.uuid4(), account_id=uuid.uuid4())
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    session_factory = MagicMock(return_value=session)

    plain_guardrail = _guardrail()  # no correction block
    with (
        patch("modulo.core.feedback_manager.get_run", AsyncMock(return_value=run)),
        patch("modulo.core.guardrails.conformance.load_node_guardrails", AsyncMock(return_value=[plain_guardrail])),
    ):
        outcome = await dispatch_reject_correction(
            session_factory=session_factory,
            org_id=_ORG,
            run_id=run.pipeline_id,
            node_id="node_a",
            node_input={"body": "x"},
            rejection_reason="nope",
            gate_id="hitl_gate_a_b",
        )

    assert outcome is None
