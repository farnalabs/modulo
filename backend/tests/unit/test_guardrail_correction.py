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
    correction = _correction(revalidation_detector_family=CorrectionDetectorFamily.REGEX.value)
    with pytest.raises(DifferentFamilyViolationError, match="does not differ"):
        correction.validate_guardrail_binding(guardrail)


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
    assert build_idempotency_key(**kwargs) == build_idempotency_key(**kwargs)


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
        guardrail=guardrail,
        backend=_Boom(),
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
        guardrail=guardrail,
        backend=_StubCorrectionBackend({}),
        state=state,
    )
    # attempt >= max_attempts and re-validation still fails -> interrupted.
    assert outcome.verdict == CorrectionVerdict.INTERRUPTED
    assert outcome.needs_human_review is True


# ---------------------------------------------------------------------------
# Concurrent-correction cap (claim-time)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal session stub returning a configurable active-correction count."""

    def __init__(self, active_count: int = 0) -> None:
        self._active = active_count

    async def execute(self, stmt: Any) -> Any:
        class _Result:
            def scalar(self) -> int:
                return self._count

            def __init__(self, count: int) -> None:
                self._count = count

        return _Result(self._active)


@pytest.mark.asyncio
async def test_claim_slot_respects_concurrency_cap():
    from modulo.core.guardrails.correction import claim_correction_slot

    correction = _correction(concurrency_cap=2)
    assert await claim_correction_slot(_FakeSession(active_count=0), org_id=_ORG, correction=correction) is True
    assert await claim_correction_slot(_FakeSession(active_count=1), org_id=_ORG, correction=correction) is True
    assert await claim_correction_slot(_FakeSession(active_count=2), org_id=_ORG, correction=correction) is False
