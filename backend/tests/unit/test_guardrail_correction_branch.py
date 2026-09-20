"""Branch-coverage tests for guardrail correction.py pure functions and validators.

Targets uncovered branches from the baseline measurement (28 BrPart).
Every test exercises the REAL function under test.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from modulo.core.eval_engine import EvalDefinition
from modulo.core.guardrails.correction import (
    CorrectionConfigError,
    CorrectionDefinition,
    CorrectionDetectorFamily,
    CorrectionOutcome,
    CorrectionRedactionPattern,
    CorrectionVerdict,
    DifferentFamilyViolationError,
    RedactCorrectBlockedError,
    RestrictedBackendViolationError,
    _coerce_redaction_pattern,
    _collect_prior_fingerprints,
    _compile_redaction_regex,
    _config_declares_schema,
    _declared_detection_type,
    _flatten_content_blocks,
    _is_strictly_worse,
    _parse_structured_output,
    _prior_metric_is_strictly_worse,
    _resolve_redaction_leaf,
    _resume_revalidation_failure,
    _revalidation_backend_error,
    build_idempotency_key,
    convergence_verdict,
    fingerprint_state,
    redact_payload,
)

# ---------------------------------------------------------------------------
# _declared_detection_type — extra branches
# ---------------------------------------------------------------------------


def test_declared_detection_type_envelope_dict_with_known_type():
    """Envelope has a known detection type → returns it."""
    config = {"detection": {"type": "regex"}}
    assert _declared_detection_type(config) == "regex"


def test_declared_detection_type_envelope_dict_unknown_type():
    """Envelope has unknown type → falls through to top-level."""
    config = {"detection": {"type": "unknown"}, "type": "regex"}
    assert _declared_detection_type(config) == "regex"


def test_declared_detection_type_envelope_not_dict():
    """Envelope is not a dict → checks top-level."""
    config = {"detection": "not-a-dict", "type": "regex"}
    assert _declared_detection_type(config) == "regex"


def test_declared_detection_type_no_known_type():
    """Neither envelope nor top-level has known type → None."""
    config = {"type": "custom", "detection": {"type": "custom"}}
    assert _declared_detection_type(config) is None


# ---------------------------------------------------------------------------
# _config_declares_schema — extra branches
# ---------------------------------------------------------------------------


def test_config_declares_schema_top_level():
    """Schema at top-level → True."""
    assert _config_declares_schema({"schema": {"type": "object"}}) is True


def test_config_declares_schema_in_envelope():
    """Schema in detection envelope → True."""
    assert _config_declares_schema({"detection": {"schema": {"type": "object"}}}) is True


def test_config_declares_schema_neither():
    """No schema → False."""
    assert _config_declares_schema({"type": "regex"}) is False


def test_config_declares_schema_envelope_not_dict():
    """Envelope not a dict → checks top-level only."""
    assert _config_declares_schema({"detection": "str"}) is False


# ---------------------------------------------------------------------------
# _revalidation_backend_error — extra branches
# ---------------------------------------------------------------------------


def test_revalidation_backend_error_non_llm_judge():
    """Non-LLM-judge family → None."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
        revalidation_detector_family=CorrectionDetectorFamily.PII,
    )
    assert _revalidation_backend_error(defn) is None


def test_revalidation_backend_error_llm_judge_no_revalidation_id():
    """LLM-judge without revalidation_model_backend_id → model_validator catches it."""
    with pytest.raises(ValueError, match="revalidation_model_backend_id"):
        CorrectionDefinition(
            id="c1",
            guardrail_id="g1",
            model_backend_id="m1",
            revalidation_detector_family=CorrectionDetectorFamily.LLM_JUDGE,
            output_schema={"type": "object"},
        )


def test_revalidation_backend_error_llm_judge_same_backend():
    """LLM-judge with same backend id → model_validator catches it."""
    with pytest.raises(ValueError, match="DIFFERENT"):
        CorrectionDefinition(
            id="c1",
            guardrail_id="g1",
            model_backend_id="m1",
            revalidation_detector_family=CorrectionDetectorFamily.LLM_JUDGE,
            revalidation_model_backend_id="m1",
            output_schema={"type": "object"},
        )


def test_revalidation_backend_error_llm_judge_different_backend():
    """LLM-judge with different backend id → passes validation."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
        revalidation_detector_family=CorrectionDetectorFamily.LLM_JUDGE,
        revalidation_model_backend_id="m2",
        output_schema={"type": "object"},
    )
    assert _revalidation_backend_error(defn) is None


# ---------------------------------------------------------------------------
# CorrectionDefinition validators
# ---------------------------------------------------------------------------


def test_correction_definition_output_schema_empty():
    """Empty output_schema → validation error."""
    with pytest.raises(ValueError, match="output_schema"):
        CorrectionDefinition(
            id="c1",
            guardrail_id="g1",
            model_backend_id="m1",
            output_schema={},
        )


def test_correction_definition_output_schema_non_dict():
    """Non-dict output_schema -> validation error."""
    with pytest.raises(ValueError, match="output_schema"):
        CorrectionDefinition(
            id="c1",
            guardrail_id="g1",
            model_backend_id="m1",
            output_schema="not-a-dict",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# CorrectionDefinition.validate_guardrail_binding
# ---------------------------------------------------------------------------


def test_guardrail_binding_redact_blocked():
    """Redaction action → RedactCorrectBlockedError."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
        revalidation_detector_family=CorrectionDetectorFamily.PII,
    )
    guardrail = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        name="g1",
        eval_type="regex",
        config={"action": "redact", "detection": {"type": "regex"}},
    )
    with pytest.raises(RedactCorrectBlockedError):
        defn.validate_guardrail_binding(guardrail)


def test_guardrail_binding_different_family_violation():
    """Same family → DifferentFamilyViolationError."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
        revalidation_detector_family=CorrectionDetectorFamily.REGEX,
        revalidation_config={"pattern": ".*", "field": "output"},
    )
    guardrail = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        name="g1",
        eval_type="regex",
        config={"detection": {"type": "regex"}},
    )
    with pytest.raises(DifferentFamilyViolationError):
        defn.validate_guardrail_binding(guardrail)


def test_guardrail_binding_valid():
    """Different family passes validation."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
        revalidation_detector_family=CorrectionDetectorFamily.PII,
    )
    guardrail = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        name="g1",
        eval_type="regex",
        config={"detection": {"type": "regex"}},
    )
    assert defn.validate_guardrail_binding(guardrail) is None


# ---------------------------------------------------------------------------
# validate_restricted_backend — extra branches
# ---------------------------------------------------------------------------


def test_validate_restricted_backend_with_privileged_capability():
    """Privileged capability → RestrictedBackendViolationError."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
    )
    with pytest.raises(RestrictedBackendViolationError):
        defn.validate_restricted_backend(["vault"])


def test_validate_restricted_backend_clean():
    """Non-privileged capability → no error."""
    defn = CorrectionDefinition(
        id="c1",
        guardrail_id="g1",
        model_backend_id="m1",
    )
    assert defn.validate_restricted_backend(["read_only_tools"]) is None


# ---------------------------------------------------------------------------
# from_eval_config
# ---------------------------------------------------------------------------


def test_from_eval_config_no_correction_block():
    """Missing 'correction' block → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="no 'correction' block"):
        CorrectionDefinition.from_eval_config({"other": "key"})


def test_from_eval_config_invalid_correction_block():
    """Invalid correction block -> raises ValidationError for missing fields."""
    with pytest.raises(ValueError, match="validation errors"):
        CorrectionDefinition.from_eval_config({"correction": {"id": ""}})


def test_from_eval_config_valid():
    """Valid correction block → parses correctly."""
    result = CorrectionDefinition.from_eval_config(
        {
            "correction": {
                "id": "c1",
                "guardrail_id": "g1",
                "model_backend_id": "m1",
                "revalidation_detector_family": "pii",
            }
        }
    )
    assert result.id == "c1"


# ---------------------------------------------------------------------------
# _coerce_redaction_pattern
# ---------------------------------------------------------------------------


def test_coerce_redaction_pattern_already_model():
    """Already a CorrectionRedactionPattern → returned as-is."""
    p = CorrectionRedactionPattern(path="a.b", pattern=".*")
    assert _coerce_redaction_pattern(p) is p


def test_coerce_redaction_pattern_from_dict():
    """Dict → coerced to model."""
    p = _coerce_redaction_pattern({"path": "a.b", "pattern": ".*"})
    assert isinstance(p, CorrectionRedactionPattern)
    assert p.path == "a.b"


# ---------------------------------------------------------------------------
# _compile_redaction_regex
# ---------------------------------------------------------------------------


def test_compile_redaction_regex_valid():
    """Valid pattern → compiled regex."""
    p = CorrectionRedactionPattern(path="a.b", pattern=r"\d+")
    result = _compile_redaction_regex(p)
    assert result is not None
    assert result.search("abc123")


def test_compile_redaction_regex_invalid():
    """Invalid pattern → None."""
    p = CorrectionRedactionPattern(path="a.b", pattern="[invalid")
    result = _compile_redaction_regex(p)
    assert result is None


# ---------------------------------------------------------------------------
# _resolve_redaction_leaf
# ---------------------------------------------------------------------------


def test_resolve_redaction_leaf_simple_path():
    """Simple dotted path → parent dict (the dict containing the leaf key)."""
    redacted = {"a": {"b": "val"}}
    result = _resolve_redaction_leaf(redacted, ["a", "b"])
    assert result == {"b": "val"}


def test_resolve_redaction_leaf_missing_intermediate():
    """Missing intermediate segment → returns the redacted root (walks up to it)."""
    redacted = {"a": {"c": "val"}}
    result = _resolve_redaction_leaf(redacted, ["a", "b"])
    # segments[:-1] = ["a"], walks into redacted["a"] = {"c": "val"}, returns it
    # The caller then does parent.get("b") which returns None
    assert result == {"c": "val"}


def test_resolve_redaction_leaf_non_dict_intermediate():
    """Non-dict intermediate → None."""
    redacted = {"a": "not-a-dict"}
    assert _resolve_redaction_leaf(redacted, ["a", "b"]) is None


def test_resolve_redaction_leaf_single_segment():
    """Single segment (leaf only) → returns redacted dict."""
    redacted = {"a": "val"}
    assert _resolve_redaction_leaf(redacted, ["a"]) == redacted


# ---------------------------------------------------------------------------
# redact_payload
# ---------------------------------------------------------------------------


def test_redact_payload_applies_regex():
    """Pattern matches → replacement applied."""
    from modulo.core.guardrails import REDACTION_MASK

    patterns = [CorrectionRedactionPattern(path="email", pattern=r"[a-z]+@")]
    result = redact_payload({"email": "alice@example.com"}, patterns)
    assert result["email"] == REDACTION_MASK + "example.com"


def test_redact_payload_no_match():
    """Pattern doesn't match → value unchanged."""
    patterns = [CorrectionRedactionPattern(path="email", pattern=r"\d{10,}")]
    result = redact_payload({"email": "alice@example.com"}, patterns)
    assert result["email"] == "alice@example.com"


def test_redact_payload_missing_path():
    """Missing path → value untouched."""
    patterns = [CorrectionRedactionPattern(path="missing.path", pattern=r".*")]
    result = redact_payload({"email": "test"}, patterns)
    assert result["email"] == "test"


def test_redact_payload_empty_segments():
    """Empty path (segments filtered to []) → skip (the loop body never runs)."""
    # Create a pattern with a path that resolves to empty segments after filtering
    # "." split gives ["", ""] but filter removes empties → []
    patterns = [CorrectionRedactionPattern(path="..", pattern=r".*")]
    result = redact_payload({"email": "test"}, patterns)
    assert result["email"] == "test"


def test_redact_payload_invalid_regex():
    """Invalid regex → pattern skipped."""
    patterns = [{"path": "email", "pattern": "[invalid"}]
    result = redact_payload({"email": "test"}, patterns)
    assert result["email"] == "test"


def test_redact_payload_non_string_leaf():
    """Non-string leaf → not replaced."""
    patterns = [CorrectionRedactionPattern(path="count", pattern=r"\d+")]
    result = redact_payload({"count": 42}, patterns)
    assert result["count"] == 42


def test_redact_payload_nested_path_missing():
    """Deep path with missing intermediate → untouched."""
    patterns = [CorrectionRedactionPattern(path="a.b.c", pattern=r".*")]
    result = redact_payload({"a": {"d": "val"}}, patterns)
    assert result["a"]["d"] == "val"


def test_redact_payload_deeply_nested():
    """Deeply nested path → replacement applied."""
    from modulo.core.guardrails import REDACTION_MASK

    patterns = [CorrectionRedactionPattern(path="a.b.secret", pattern=r"\d+")]
    result = redact_payload({"a": {"b": {"secret": "code123"}}}, patterns)
    assert result["a"]["b"]["secret"] == "code" + REDACTION_MASK


# ---------------------------------------------------------------------------
# _collect_prior_fingerprints — extra branches
# ---------------------------------------------------------------------------


def test_collect_prior_fingerprints_non_dict_state():
    """Non-dict state → skipped."""
    assert not _collect_prior_fingerprints(["not-a-dict", 42])


def test_collect_prior_fingerprints_empty():
    """Empty list → empty set."""
    assert not _collect_prior_fingerprints([])


def test_collect_prior_fingerprints_both_keys():
    """Both input and output fingerprints collected."""
    states = [
        {"input_fingerprint": "in1", "output_fingerprint": "out1"},
    ]
    result = _collect_prior_fingerprints(states)
    assert result == {"in1", "out1"}


def test_collect_prior_fingerprints_non_string_value():
    """Non-string fingerprint value → skipped."""
    states = [{"input_fingerprint": 42}]
    assert not _collect_prior_fingerprints(states)


# ---------------------------------------------------------------------------
# _is_strictly_worse
# ---------------------------------------------------------------------------


def test_is_strictly_worse_true():
    assert _is_strictly_worse({"severity": 3}, {"severity": 1}) is True


def test_is_strictly_worse_equal():
    assert _is_strictly_worse({"severity": 1}, {"severity": 1}) is False


def test_is_strictly_worse_less():
    assert _is_strictly_worse({"severity": 1}, {"severity": 3}) is False


def test_is_strictly_worse_missing_severity():
    """Missing severity → treated as 0."""
    assert _is_strictly_worse({"severity": 1}, {}) is True


# ---------------------------------------------------------------------------
# _prior_metric_is_strictly_worse — extra branches
# ---------------------------------------------------------------------------


def test_prior_metric_is_strictly_worse_non_dict_state():
    """Non-dict state → skipped."""
    assert _prior_metric_is_strictly_worse({"severity": 5}, ["not-a-dict"]) is False


def test_prior_metric_is_strictly_worse_all_metric_keys():
    """Checks all three metric keys."""
    states = [
        {"input_violation_metric": {"severity": 1}},
        {"output_violation_metric": {"severity": 1}},
        {"violation_metric": {"severity": 1}},
    ]
    assert _prior_metric_is_strictly_worse({"severity": 3}, states) is True


def test_prior_metric_is_strictly_worse_non_dict_metric():
    """Non-dict metric → skipped."""
    states = [{"violation_metric": "not-a-dict"}]
    assert _prior_metric_is_strictly_worse({"severity": 3}, states) is False


def test_prior_metric_is_strictly_worse_empty():
    assert _prior_metric_is_strictly_worse({"severity": 1}, []) is False


# ---------------------------------------------------------------------------
# convergence_verdict — extra branches
# ---------------------------------------------------------------------------


def test_convergence_verdict_input_seen():
    """Previously-seen input → CONVERGED."""
    input_payload = {"key": "val"}
    fp = fingerprint_state(input_payload)
    prior = [{"input_fingerprint": fp}]
    verdict = convergence_verdict(
        redacted_input=input_payload,
        produced_output=None,
        prior_states=prior,
    )
    assert verdict == CorrectionVerdict.CONVERGED


def test_convergence_verdict_output_seen():
    """Previously-seen output → CONVERGED."""
    output = {"result": "ok"}
    fp = fingerprint_state(output)
    prior = [{"output_fingerprint": fp}]
    verdict = convergence_verdict(
        redacted_input={"key": "val"},
        produced_output=output,
        prior_states=prior,
    )
    assert verdict == CorrectionVerdict.CONVERGED


def test_convergence_verdict_strictly_worse():
    """Strictly worse metric → CONVERGED."""
    prior = [{"violation_metric": {"severity": 1}}]
    verdict = convergence_verdict(
        redacted_input={"key": "val"},
        produced_output=None,
        prior_states=prior,
        current_violation_metric={"severity": 3},
    )
    assert verdict == CorrectionVerdict.CONVERGED


def test_convergence_verdict_fresh():
    """Fresh state → None."""
    verdict = convergence_verdict(
        redacted_input={"fresh": True},
        produced_output=None,
        prior_states=[],
    )
    assert verdict is None


# ---------------------------------------------------------------------------
# build_idempotency_key
# ---------------------------------------------------------------------------


def test_build_idempotency_key_deterministic():
    """Same inputs → same key."""
    args = {
        "org_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "node_id": "n1",
        "correction_id": "c1",
        "redacted_input": {"a": 1},
    }
    k1 = build_idempotency_key(**args)
    k2 = build_idempotency_key(**args)
    assert k1 == k2


def test_build_idempotency_key_different_inputs():
    """Different inputs → different key."""
    org = uuid.uuid4()
    run = uuid.uuid4()
    k1 = build_idempotency_key(org_id=org, run_id=run, node_id="n1", correction_id="c1", redacted_input={"a": 1})
    k2 = build_idempotency_key(org_id=org, run_id=run, node_id="n1", correction_id="c1", redacted_input={"a": 2})
    assert k1 != k2


# ---------------------------------------------------------------------------
# fingerprint_state
# ---------------------------------------------------------------------------


def test_fingerprint_state_order_independent():
    """Key ordering does not affect fingerprint."""
    fp1 = fingerprint_state({"b": 2, "a": 1})
    fp2 = fingerprint_state({"a": 1, "b": 2})
    assert fp1 == fp2


def test_fingerprint_state_deterministic():
    """Same input → same fingerprint."""
    fp1 = fingerprint_state({"x": "y"})
    fp2 = fingerprint_state({"x": "y"})
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# _parse_structured_output
# ---------------------------------------------------------------------------


def test_parse_structured_output_valid_json():
    """Valid JSON matching schema → parsed dict."""
    result = _parse_structured_output('{"key": "val"}', {"type": "object"})
    assert result == {"key": "val"}


def test_parse_structured_output_empty_string():
    """Empty string → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="empty output"):
        _parse_structured_output("", {})


def test_parse_structured_output_whitespace_only():
    """Whitespace-only → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="empty output"):
        _parse_structured_output("   \n  ", {})


def test_parse_structured_output_none():
    """None → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="empty output"):
        _parse_structured_output(None, {})


def test_parse_structured_output_invalid_json():
    """Invalid JSON → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="invalid JSON"):
        _parse_structured_output("{not json}", {})


def test_parse_structured_output_not_dict():
    """JSON list → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="must be a JSON object"):
        _parse_structured_output("[1, 2, 3]", {})


def test_parse_structured_output_schema_violation():
    """Valid JSON but violates schema → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="violates output_schema"):
        _parse_structured_output(
            '{"key": "val"}',
            {"type": "object", "required": ["missing_key"]},
        )


def test_parse_structured_output_no_schema():
    """Valid JSON with empty schema → parsed."""
    result = _parse_structured_output('{"key": "val"}', {})
    assert result == {"key": "val"}


def test_parse_structured_output_malformed_schema():
    """Valid JSON but malformed schema → CorrectionConfigError."""
    with pytest.raises(CorrectionConfigError, match="malformed"):
        _parse_structured_output('{"key": "val"}', {"type": "invalid_type_xyz"})


# ---------------------------------------------------------------------------
# _flatten_content_blocks
# ---------------------------------------------------------------------------


def test_flatten_content_blocks_string():
    """String content → stringified."""
    assert _flatten_content_blocks("hello") == "hello"


def test_flatten_content_blocks_list_of_strings():
    """List of strings → joined."""
    assert _flatten_content_blocks(["a", "b", "c"]) == "a\nb\nc"


def test_flatten_content_blocks_list_of_dicts():
    """List of dicts with 'text' → joined."""
    blocks = [{"text": "hello"}, {"text": "world"}]
    assert _flatten_content_blocks(blocks) == "hello\nworld"


def test_flatten_content_blocks_mixed_list():
    """Mixed string and dict blocks → non-string/non-text items skipped."""
    blocks = ["a", {"text": "b"}, 42]
    assert _flatten_content_blocks(blocks) == "a\nb"


def test_flatten_content_blocks_empty_list():
    """Empty list → empty string."""
    assert not _flatten_content_blocks([])


def test_flatten_content_blocks_non_string_dict_text():
    """Dict with non-string 'text' → skipped."""
    blocks = [{"text": 42}]
    assert not _flatten_content_blocks(blocks)


def test_flatten_content_blocks_dict_without_text():
    """Dict without 'text' key → skipped."""
    blocks = [{"other": "key"}]
    assert not _flatten_content_blocks(blocks)


# ---------------------------------------------------------------------------
# _resume_revalidation_failure
# ---------------------------------------------------------------------------


def test_resume_revalidation_failure_budget_exhausted():
    """Budget exhausted → INTERRUPTED."""
    revalidation = MagicMock()
    result = _resume_revalidation_failure({"output": "val"}, revalidation, {}, "detail", budget_exhausted=True)
    assert result.verdict == CorrectionVerdict.INTERRUPTED
    assert result.needs_human_review is True


def test_resume_revalidation_failure_budget_not_exhausted():
    """Budget not exhausted → STILL_VIOLATING."""
    revalidation = MagicMock()
    result = _resume_revalidation_failure({"output": "val"}, revalidation, {}, "detail", budget_exhausted=False)
    assert result.verdict == CorrectionVerdict.STILL_VIOLATING
    assert result.needs_human_review is True


# ---------------------------------------------------------------------------
# CorrectionRedactionPattern — default replacement
# ---------------------------------------------------------------------------


def test_redaction_pattern_default_replacement():
    """Default replacement is REDACTION_MASK."""
    from modulo.core.guardrails import REDACTION_MASK

    p = CorrectionRedactionPattern(path="a.b", pattern=".*")
    assert p.replacement == REDACTION_MASK


# ---------------------------------------------------------------------------
# CorrectionOutcome — dataclass defaults
# ---------------------------------------------------------------------------


def test_correction_outcome_defaults():
    """Default field values."""
    outcome = CorrectionOutcome(verdict=CorrectionVerdict.RESOLVED)
    assert not outcome.detail
    assert outcome.produced_output is None
    assert outcome.revalidation_result is None
    assert outcome.needs_human_review is False
    assert not outcome.state
