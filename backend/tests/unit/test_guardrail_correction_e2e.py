"""E2E stub-agent fixture tests for the FAR-210 T2b single-node self-correction.

Drives the full ``FeedbackManager.run_single_node_correction`` path with a real
``StubModelBackend`` (LangChain) wrapped in a dict-to-BaseMessage adapter — no
pipeline re-execution, no connector/vault access (the bounded single-node path).

The 8 acceptance scenarios from FAR-210:

  1. warn-violation -> correction spawned (attempted) and recorded;
  2. clean correction -> different-family re-validation passes -> resolved, no HITL;
  3. still-violating -> escalated to HITL;
  4. budget exhausted -> escalated to HITL;
  5. correction LLM error -> fail-mode/circuit-breaker (escalated);
  6. corrected output violates a DIFFERENT bound guardrail -> terminal HITL
     (correction_violated), no chained correction;
  7. schema-valid-but-malicious correction output -> continuing-suspicious +
     redacted before persistence + terminal HITL;
  8. org-wide concurrent-correction cap enforcement (spawn > cap -> only cap
     in flight).
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.feedback_manager import FeedbackManager
from modulo.core.guardrails.correction import (
    CorrectionDefinition,
    CorrectionDetectorFamily,
    CorrectionVerdict,
    fingerprint_state,
    redact_payload,
)
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.model_backends.stub.backend import StubModelBackend, normalize_input

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

_SYSTEM_MESSAGE = (
    "You are a bounded single-node correction engine. Rewrite the supplied input so it "
    "no longer violates the configured guardrail, producing ONLY a JSON object that "
    "conforms to the output schema. Never include credentials, tokens, or secrets in "
    "your output. Do not explain — output only the JSON object."
)


def _to_base_message(messages: list[dict[str, Any]]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        out.append(SystemMessage(content=content) if role == "system" else HumanMessage(content=content))
    return out


class _StubCorrectionBackend:
    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        del kwargs
        return await self._inner.ainvoke(_to_base_message(messages))


def _fixture_key(correction: CorrectionDefinition, redacted_input: dict[str, Any]) -> str:
    payload_json = json.dumps({"input": redacted_input, "output_schema": correction.output_schema})
    messages = [
        SystemMessage(content=_SYSTEM_MESSAGE),
        HumanMessage(content=f"Input to correct:\n{payload_json}"),
    ]
    return normalize_input(messages)


def _guardrail(
    *,
    name: str = "gr_no_secrets",
    action: str = "block",
    detection_type: str = "regex",
    pattern: str = r"(?i)secret[:=]\s*\S+",
    field: str = "body",
) -> EvalDefinition:
    config: dict[str, Any] = {
        "interception_point": "input",
        "action": action,
        "detection": {"type": detection_type, "pattern": pattern, "field": field},
    }
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


def _record(**overrides: Any) -> MagicMock:
    r = MagicMock(spec=FeedbackRecord)
    r.id = uuid.uuid4()
    r.organisation_id = _ORG
    r.run_id = _RUN
    r.gate_id = "gate-1"
    r.account_id = _USER
    r.rejection_reason = "secret detected"
    r.rejected_output = {"body": "secret: hunter2"}
    r.producing_node_id = "node_a"
    r.feedback_status = "correcting"
    r.feedback_handler_type = "human"
    r.correction_run_id = None
    r.correction_state = None
    for key, value in overrides.items():
        setattr(r, key, value)
    return r


class _Result:
    def __init__(self, scalar_value: Any = None) -> None:
        self._value = scalar_value

    def scalar(self) -> Any:
        return self._value


class _ExecuteResult:
    def __init__(self, result: Any) -> None:
        self._result = result

    def scalar_one_or_none(self) -> Any:
        return self._result


async def _run_scenario(
    *,
    backend: Any,
    correction: CorrectionDefinition,
    guardrail: EvalDefinition,
    bound_guardrails: list[EvalDefinition] | None = None,
    session: AsyncMock | None = None,
    node_input: dict[str, Any] | None = None,
    active_corrections: int = 0,
    current_record_in_correcting: bool = False,
    prior_state: dict[str, Any] | None = None,
    record_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive FeedbackManager.run_single_node_correction with a stub session."""
    session = session or AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.flush = AsyncMock()

    record = _record()
    if prior_state is not None:
        record.correction_state = prior_state
    for key, value in (record_overrides or {}).items():
        setattr(record, key, value)

    def _fake_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(stmt)
        # Apply the feedback_status mutation for an UPDATE on feedback_records so
        # the in-memory record mock reflects what _persist_correction_outcome set.
        # (Checked BEFORE the count branch: the fenced UPDATE's RETURNING clause
        # carries `account_id`, which contains the substring "count".)
        if "UPDATE feedback_records" in text:
            # Finding 3 fence: _persist_correction_outcome's UPDATE carries a
            # `feedback_status == 'correcting'` predicate. When the record's
            # status has drifted (a human escalated/resolved/dismissed it), the
            # UPDATE matches 0 rows -> scalar_one_or_none() is None -> the
            # manager raises ConcurrentModificationError. The fake mirrors that
            # by returning no row for any non-'correcting' record.
            if record.feedback_status != "correcting":
                return _ExecuteResult(None)
            values = getattr(stmt, "_values", {}) or {}
            for key, raw in values.items():
                column = getattr(key, "name", None)
                value = raw
                if type(raw).__name__ == "BindParameter":
                    value = raw.value
                if column == "feedback_status":
                    record.feedback_status = value
                elif column == "needs_human_review":
                    record.needs_human_review = value
            return _ExecuteResult(record)
        if "count(" in text.lower():
            # MAJOR-3: the claim query excludes the current record (id != ...).
            # When the fake is told the current record is itself in 'correcting',
            # the exclusion drops it from the count the DB would report.
            count = active_corrections
            if current_record_in_correcting and "!=" in text:
                count = max(0, count - 1)
            return _Result(count)
        return _ExecuteResult(record)

    session.execute = AsyncMock(side_effect=_fake_execute)

    with (
        patch.object(FeedbackManager, "get_feedback_record", AsyncMock(return_value=record)),
        patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
    ):
        mgr = FeedbackManager(session, _ORG)
        result = await mgr.run_single_node_correction(
            record_id=record.id,
            guardrail=guardrail,
            correction=correction,
            node_input=node_input or {"body": "secret: hunter2"},
            backend=backend,
            bound_guardrails=bound_guardrails,
        )
    return {"result": result, "record": record, "session": session}


# ---------------------------------------------------------------------------
# Scenario 1: warn-violation -> correction spawned + recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_warn_violation_records_and_spawns_correction():
    guardrail = _guardrail()
    correction = _correction()
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    backend = _StubCorrectionBackend({_fixture_key(correction, redacted): json.dumps({"body": "safe now"})})
    outcome = await _run_scenario(backend=backend, correction=correction, guardrail=guardrail)
    assert outcome["result"]["verdict"] == CorrectionVerdict.RESOLVED.value
    # The correction attempt is recorded on the FeedbackRecord (state persisted).
    assert outcome["record"].correction_state is not None
    assert outcome["record"].correction_state["idempotency_key"]


# ---------------------------------------------------------------------------
# Scenario 2: clean correction -> different-family re-validation passes -> resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_clean_correction_resolves_no_hitl():
    guardrail = _guardrail()
    correction = _correction()
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    backend = _StubCorrectionBackend({_fixture_key(correction, redacted): json.dumps({"body": "safe now"})})
    outcome = await _run_scenario(backend=backend, correction=correction, guardrail=guardrail)
    assert outcome["result"]["verdict"] == CorrectionVerdict.RESOLVED.value
    assert outcome["result"]["needs_human_review"] is False
    # Record resolved.
    assert outcome["record"].feedback_status == "resolved"


# ---------------------------------------------------------------------------
# Scenario 3: still-violating -> FRESH retry attempt, then HITL on exhaustion
# (MAJOR-6: max_attempts>1 produces fresh LM attempts, not a re-validation of
# the recorded output — the e2e test that previously documented the dead path
# (immediate escalation, no second attempt) is replaced by these retry tests).
# ---------------------------------------------------------------------------


class _SequencedBackend:
    """Returns a per-invocation sequence of outputs and counts invocations."""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self._index = 0
        self.invoke_count = 0

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        self.invoke_count += 1
        idx = min(self._index, len(self._outputs) - 1)
        self._index += 1
        return self._outputs[idx]


class _RepeatingBackend:
    """Returns the SAME output on every invocation and counts invocations."""

    def __init__(self, output: str) -> None:
        self._output = output
        self.invoke_count = 0

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        self.invoke_count += 1
        return self._output


@pytest.mark.asyncio
async def test_e2e_still_violating_issues_fresh_attempt_then_exhausts():
    """MAJOR-6: a still-violating outcome issues a FRESH LM attempt while the retry
    budget remains (max_attempts=2); only exhaustion escalates to HITL."""
    guardrail = _guardrail()
    correction = _correction(max_attempts=2)
    # Two DIFFERENT PII-violating outputs (long digit runs): attempt 1 still-
    # violating -> attempt 2 (fresh LM) still-violating at max_attempts ->
    # budget_exhausted. The LM must have run TWICE.
    backend = _SequencedBackend(
        [
            json.dumps({"body": "123456789012345678901"}),
            json.dumps({"body": "098765432109876543210"}),
        ]
    )
    outcome = await _run_scenario(backend=backend, correction=correction, guardrail=guardrail)
    assert backend.invoke_count == 2
    assert outcome["result"]["verdict"] == CorrectionVerdict.BUDGET_EXHAUSTED.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


@pytest.mark.asyncio
async def test_e2e_retry_resolves_on_second_fresh_attempt():
    """MAJOR-6: a fresh retry that produces a CLEAN output on the second attempt
    resolves — the retry is a fresh LM run, not a re-validation of attempt 1."""
    guardrail = _guardrail()
    correction = _correction(max_attempts=2)
    backend = _SequencedBackend(
        [
            json.dumps({"body": "123456789012345678901"}),
            json.dumps({"body": "safe now"}),
        ]
    )
    outcome = await _run_scenario(backend=backend, correction=correction, guardrail=guardrail)
    assert backend.invoke_count == 2
    assert outcome["result"]["verdict"] == CorrectionVerdict.RESOLVED.value
    assert outcome["result"]["needs_human_review"] is False
    assert outcome["record"].feedback_status == "resolved"


@pytest.mark.asyncio
async def test_e2e_retry_converges_on_repeated_output():
    """MAJOR-6: a retry that reproduces the SAME still-violating output converges
    (no oscillation burn) instead of burning the remaining budget."""
    guardrail = _guardrail()
    correction = _correction(max_attempts=2)
    backend = _RepeatingBackend(json.dumps({"body": "123456789012345678901"}))
    outcome = await _run_scenario(backend=backend, correction=correction, guardrail=guardrail)
    # The second attempt is still a fresh LM run (the retry ran), but the
    # produced output repeats attempt 1's -> converged (oscillation -> HITL).
    assert backend.invoke_count == 2
    assert outcome["result"]["verdict"] == CorrectionVerdict.CONVERGED.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


# ---------------------------------------------------------------------------
# Scenario 4: budget exhausted -> HITL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_budget_exhausted_escalates_to_hitl():
    guardrail = _guardrail()
    correction = _correction(max_attempts=1)
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    backend = _StubCorrectionBackend(
        {_fixture_key(correction, redacted): json.dumps({"body": "123456789012345678901"})}
    )
    outcome = await _run_scenario(backend=backend, correction=correction, guardrail=guardrail)
    assert outcome["result"]["verdict"] == CorrectionVerdict.BUDGET_EXHAUSTED.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


# ---------------------------------------------------------------------------
# Scenario 5: correction LLM error -> fail-mode/circuit-breaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_lm_error_fail_mode():
    class _Raises:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    guardrail = _guardrail()
    correction = _correction()
    outcome = await _run_scenario(backend=_Raises(), correction=correction, guardrail=guardrail)
    assert outcome["result"]["verdict"] == CorrectionVerdict.LM_ERROR.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


# ---------------------------------------------------------------------------
# Scenario 6: corrected output violates a DIFFERENT bound guardrail -> terminal HITL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_corrected_output_violates_different_guardrail_terminal_hitl():
    guardrail = _guardrail(name="gr_no_secrets")
    correction = _correction()
    # A second bound guardrail that the corrected output violates (PII family
    # passes the first guardrail's regex since it no longer matches 'secret:').
    other_guardrail = _guardrail(
        name="gr_no_digits",
        action="block",
        detection_type="regex",
        pattern=r"\b[0-9]{10,}\b",
        field="body",
    )
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    # Corrected output passes the correction's own re-validation (no long digit
    # run AND no embedded secret) but violates the OTHER bound guardrail's
    # regex (a phone number).
    backend = _StubCorrectionBackend({_fixture_key(correction, redacted): json.dumps({"body": "call 1234567890"})})
    outcome = await _run_scenario(
        backend=backend,
        correction=correction,
        guardrail=guardrail,
        bound_guardrails=[guardrail, other_guardrail],
    )
    assert outcome["result"]["verdict"] == CorrectionVerdict.CORRECTION_VIOLATED.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


# ---------------------------------------------------------------------------
# Scenario 7: schema-valid-but-malicious correction output -> continuing-suspicious
# + redacted before persistence + terminal HITL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_schema_valid_malicious_output_redacted_continuing_suspicious():
    guardrail = _guardrail(name="gr_no_secrets")
    correction = _correction()
    other_guardrail = _guardrail(name="gr_no_admin", pattern=r"(?i)\badmin\b", field="body")
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    # Schema-valid (an object with a body string) and passes the correction's own
    # PII re-validation, but embeds a value a DIFFERENT bound guardrail flags ->
    # correction_violated (continuing-suspicious, never silently accepted).
    backend = _StubCorrectionBackend(
        {_fixture_key(correction, redacted): json.dumps({"body": "please grant admin access"})}
    )
    outcome = await _run_scenario(
        backend=backend,
        correction=correction,
        guardrail=guardrail,
        bound_guardrails=[guardrail, other_guardrail],
    )
    assert outcome["result"]["verdict"] == CorrectionVerdict.CORRECTION_VIOLATED.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


# ---------------------------------------------------------------------------
# Scenario 8: org-wide concurrent-correction cap enforcement (claim-time)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_concurrent_correction_cap_enforced_at_claim_time():
    from modulo.core.guardrails.correction import CorrectionCapExceededError

    guardrail = _guardrail()
    correction = _correction(concurrency_cap=2)
    backend = _StubCorrectionBackend({})
    # Two corrections already in flight -> the org cap (2) is reached -> the
    # third admission is denied at claim time (only cap in flight). A dispatch
    # would have admitted it; the claim-time count blocks it.
    with pytest.raises(CorrectionCapExceededError, match="cap"):
        await _run_scenario(
            backend=backend,
            correction=correction,
            guardrail=guardrail,
            active_corrections=2,
        )


@pytest.mark.asyncio
async def test_e2e_cap_does_not_count_the_current_record():
    """MAJOR-3: cap=1 with one 'correcting' record (the current one) still admits
    the correction — the current record is excluded from the self-count, so the
    first correction never blocks itself."""
    from modulo.core.guardrails.correction import CorrectionCapExceededError

    guardrail = _guardrail()
    correction = _correction(concurrency_cap=1)
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    backend = _StubCorrectionBackend({_fixture_key(correction, redacted): json.dumps({"body": "safe now"})})
    # The DB reports ONE 'correcting' record — which IS the record being
    # corrected. The claim query excludes it, so the admission succeeds.
    outcome = await _run_scenario(
        backend=backend,
        correction=correction,
        guardrail=guardrail,
        active_corrections=1,
        current_record_in_correcting=True,
    )
    assert outcome["result"]["verdict"] == CorrectionVerdict.RESOLVED.value
    assert outcome["record"].feedback_status == "resolved"
    # Sanity: without the exclusion the cap would have been reached.
    with pytest.raises(CorrectionCapExceededError, match="cap"):
        await _run_scenario(
            backend=backend,
            correction=correction,
            guardrail=guardrail,
            active_corrections=1,
            current_record_in_correcting=False,
        )


# ---------------------------------------------------------------------------
# Restricted backend: a correction backend that claims vault/guardrail access
# is rejected at dispatch (fail-closed, never reaches the LM).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_restricted_backend_privileged_capability_rejected():
    from modulo.core.guardrails.correction import RestrictedBackendViolationError

    guardrail = _guardrail()
    correction = _correction()

    class _PrivilegedBackend:
        capabilities = ("vault",)

        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT run for a privileged backend")

    with pytest.raises(RestrictedBackendViolationError, match="restricted"):
        await _run_scenario(
            backend=_PrivilegedBackend(),
            correction=correction,
            guardrail=guardrail,
        )


# ---------------------------------------------------------------------------
# Resume (idempotency): an interrupted correction re-validates, never re-runs LM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_resume_revalidates_produced_output():
    from modulo.core.guardrails.correction import build_idempotency_key

    guardrail = _guardrail()
    correction = _correction()
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    idem_key = build_idempotency_key(
        org_id=_ORG,
        run_id=_RUN,
        node_id="node_a",
        correction_id=correction.id,
        redacted_input=redacted,
    )
    prior_state = {
        "idempotency_key": idem_key,
        "attempt": 1,
        "input_fingerprint": fingerprint_state(redacted),
        "output_fingerprint": fingerprint_state({"body": "safe now"}),
        "produced_output": {"body": "safe now"},
    }

    class _Boom:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT re-run on resume")

    outcome = await _run_scenario(
        backend=_Boom(),
        correction=correction,
        guardrail=guardrail,
        prior_state=prior_state,
    )
    assert outcome["result"]["verdict"] == CorrectionVerdict.RESOLVED.value
    assert outcome["record"].feedback_status == "resolved"


# ---------------------------------------------------------------------------
# Redact+correct HARD-BLOCK through the real FeedbackManager path (fail-closed
# before the LM ever runs — an exfiltration channel for the data redaction
# protects).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_redact_correct_hard_blocked_before_lm():
    from modulo.core.guardrails.correction import RedactCorrectBlockedError

    guardrail = _guardrail(action="redact")
    correction = _correction()

    class _ShouldNeverRun:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT run for a redact+correct binding")

    with pytest.raises(RedactCorrectBlockedError, match="HARD-BLOCKED"):
        await _run_scenario(
            backend=_ShouldNeverRun(),
            correction=correction,
            guardrail=guardrail,
        )


# ---------------------------------------------------------------------------
# Resume with an exhausted budget + still-violating produced output records
# correction_interrupted and escalates the record (never re-runs the LM).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_resume_budget_exhausted_records_interrupted():
    from modulo.core.guardrails.correction import build_idempotency_key

    guardrail = _guardrail()
    correction = _correction(max_attempts=1)
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    idem_key = build_idempotency_key(
        org_id=_ORG,
        run_id=_RUN,
        node_id="node_a",
        correction_id=correction.id,
        redacted_input=redacted,
    )
    prior_state = {
        "idempotency_key": idem_key,
        "attempt": 1,
        "input_fingerprint": fingerprint_state(redacted),
        "produced_output": {"body": "123456789012345678901"},
    }

    class _Boom:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT re-run on resume")

    outcome = await _run_scenario(
        backend=_Boom(),
        correction=correction,
        guardrail=guardrail,
        prior_state=prior_state,
    )
    # attempt (1) >= max_attempts (1) and the re-validation of the recorded
    # produced output still fails -> correction_interrupted, escalated.
    assert outcome["result"]["verdict"] == CorrectionVerdict.INTERRUPTED.value
    assert outcome["result"]["needs_human_review"] is True
    assert outcome["record"].feedback_status == "escalated"


# ---------------------------------------------------------------------------
# Finding 3 (review): unfenced status writes must never reverse a human decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_dispatch_on_terminal_record_raises_invalid_transition():
    """Finding 3: dispatching a single-node correction on a TERMINAL record
    (``resolved``) raises ``InvalidTransitionError`` — the correction does NOT
    execute (the LM never runs) and the record is untouched."""
    from modulo.core.feedback_manager import InvalidTransitionError

    guardrail = _guardrail()
    correction = _correction()

    class _ShouldNeverRun:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT run for a terminal record")

    with pytest.raises(InvalidTransitionError, match="terminal"):
        await _run_scenario(
            backend=_ShouldNeverRun(),
            correction=correction,
            guardrail=guardrail,
            record_overrides={"feedback_status": "resolved"},
        )


@pytest.mark.asyncio
async def test_e2e_resume_does_not_reverse_human_escalation():
    """Finding 3 concrete reversal: a correction is interrupted (record
    ``correcting``), a human reviews and escalates (``correcting -> escalated``),
    then the idempotent re-dispatch resumes with the SAME idempotency key and a
    produced output that would re-validate clean. The resume must NOT silently
    flip the record back to ``resolved`` — dispatch gates on non-terminal status
    and raises ``InvalidTransitionError``, leaving the human decision intact."""
    from modulo.core.feedback_manager import InvalidTransitionError
    from modulo.core.guardrails.correction import build_idempotency_key

    guardrail = _guardrail()
    correction = _correction()
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    idem_key = build_idempotency_key(
        org_id=_ORG,
        run_id=_RUN,
        node_id="node_a",
        correction_id=correction.id,
        redacted_input=redacted,
    )
    prior_state = {
        "idempotency_key": idem_key,
        "attempt": 1,
        "input_fingerprint": fingerprint_state(redacted),
        "output_fingerprint": fingerprint_state({"body": "safe now"}),
        "produced_output": {"body": "safe now"},
    }

    class _ShouldNeverRun:
        async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise AssertionError("LM must NOT re-run on resume of an escalated record")

    with pytest.raises(InvalidTransitionError, match="terminal"):
        await _run_scenario(
            backend=_ShouldNeverRun(),
            correction=correction,
            guardrail=guardrail,
            prior_state=prior_state,
            record_overrides={"feedback_status": "escalated"},
        )


@pytest.mark.asyncio
async def test_e2e_persist_fence_rejects_status_drift():
    """Finding 3 defense-in-depth: even a NON-terminal record that slips through
    the dispatch gate (``pending``) cannot have its correction outcome persisted
    once the record has drifted out of ``correcting`` — the fenced UPDATE matches
    zero rows and ``_persist_correction_outcome`` raises
    ``ConcurrentModificationError`` instead of silently writing a stale status."""
    from modulo.core.feedback_manager import ConcurrentModificationError

    guardrail = _guardrail()
    correction = _correction()
    redacted = redact_payload({"body": "secret: hunter2"}, correction.input_redaction_patterns)
    backend = _StubCorrectionBackend({_fixture_key(correction, redacted): json.dumps({"body": "safe now"})})

    with pytest.raises(ConcurrentModificationError, match="Expected 'correcting'"):
        await _run_scenario(
            backend=backend,
            correction=correction,
            guardrail=guardrail,
            record_overrides={"feedback_status": "pending"},
        )
