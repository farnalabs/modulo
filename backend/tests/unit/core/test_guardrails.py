"""Unit tests for modulo.core.guardrails — ingestion-edge guardrail machinery.

Covers the pure detection, masks-only redaction, static path resolution,
two-phase pass, block semantics, allowlist, and conformance derivation.
"""

import uuid

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType, GuardrailMisroutedError
from modulo.core.guardrails import (
    REDACTION_MASK,
    FieldRedactionMode,
    FieldRedactionPolicy,
    GuardrailBlockedError,
    GuardrailConfigError,
    apply_redaction_masks,
    derive_conformance_state,
    evaluate_guardrails,
    resolve_static_path,
    run_guardrail_pass,
    run_interception_pass,
    set_static_path,
)

_ORG_ID = uuid.uuid4()


def _guardrail(
    *,
    name: str = "gr",
    action: str = "block",
    detection: dict | None = None,
    failure_behaviour: str = "block",
    redaction: list | None = None,
    required_capabilities: list | None = None,
) -> EvalDefinition:
    config: dict = {"action": action, "interception_point": "input"}
    if redaction is not None:
        config["redaction"] = redaction
    if required_capabilities is not None:
        config["required_capabilities"] = required_capabilities
    if detection:
        config.update(detection)
    else:
        config.setdefault("type", "regex")
        config.setdefault("field", "body")
        config.setdefault("pattern", r"SECRET_[A-Z0-9]{8}")
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour=failure_behaviour,
    )


# ---------------------------------------------------------------------------
# Static field-path resolution (EXACT/ANCHOR — substring forbidden)
# ---------------------------------------------------------------------------


def test_resolve_static_path_exact_nested():
    payload = {"config": {"credentials": {"api_key": "abc"}}, "body": "text"}
    found, value = resolve_static_path(payload, "config.credentials.api_key")
    assert found
    assert value == "abc"


def test_resolve_static_path_absent_segment_is_not_found():
    payload = {"config": {"credentials": {"api_key": "abc"}}}
    assert resolve_static_path(payload, "config.credentials.secret") == (False, None)
    assert resolve_static_path(payload, "config.missing") == (False, None)


def test_resolve_static_path_never_substring_matches():
    payload = {"api_key_legacy": "abc", "body": "contains api_key_legacy"}
    # Substring-style matching would find 'api_key' inside 'api_key_legacy';
    # exact matching must NOT.
    assert resolve_static_path(payload, "api_key") == (False, None)


def test_set_static_path_only_updates_existing_keys():
    payload = {"config": {"credentials": {"api_key": "abc"}}}
    assert set_static_path(payload, "config.credentials.api_key", "masked") is True
    assert payload["config"]["credentials"]["api_key"] == "masked"
    # Missing intermediate segments are never created.
    assert set_static_path(payload, "config.new.nested", "x") is False
    assert "new" not in payload["config"]


# ---------------------------------------------------------------------------
# Redaction (masks-only)
# ---------------------------------------------------------------------------


def test_apply_redaction_masks_transform_uses_fixed_mask():
    payload = {"credentials": {"api_key": "sk-live-123"}, "body": "keep me"}
    redacted, entries = apply_redaction_masks(
        payload,
        [FieldRedactionPolicy(path="credentials.api_key", mode=FieldRedactionMode.TRANSFORM)],
    )
    assert redacted["credentials"]["api_key"] == REDACTION_MASK
    assert redacted["body"] == "keep me"
    assert entries[0].applied
    assert entries[0].reason == "masked"


def test_apply_redaction_masks_does_not_mutate_original():
    payload = {"credentials": {"api_key": "sk-live-123"}}
    redacted, _ = apply_redaction_masks(
        payload,
        [FieldRedactionPolicy(path="credentials.api_key")],
    )
    assert payload["credentials"]["api_key"] == "sk-live-123"
    assert redacted["credentials"]["api_key"] == REDACTION_MASK


def test_apply_redaction_masks_drop_removes_key():
    payload = {"credentials": {"api_key": "sk-live-123", "name": "prod"}}
    redacted, entries = apply_redaction_masks(
        payload,
        [FieldRedactionPolicy(path="credentials.api_key", mode=FieldRedactionMode.DROP)],
    )
    assert "api_key" not in redacted["credentials"]
    assert redacted["credentials"]["name"] == "prod"
    assert entries[0].reason == "dropped"


def test_apply_redaction_masks_block_policy_raises_when_present():
    payload = {"credentials": {"api_key": "sk-live-123"}}
    with pytest.raises(GuardrailBlockedError):
        apply_redaction_masks(
            payload,
            [FieldRedactionPolicy(path="credentials.api_key", mode=FieldRedactionMode.BLOCK)],
            raise_on_block=True,
        )


def test_apply_redaction_masks_allowlist_never_touched():
    payload = {
        "run_id": "abc",
        "organisation_id": "org-1",
        "credentials": {"api_key": "sk-live-123"},
    }
    redacted, entries = apply_redaction_masks(
        payload,
        [
            FieldRedactionPolicy(path="run_id"),
            FieldRedactionPolicy(path="credentials.api_key"),
        ],
    )
    assert redacted["run_id"] == "abc"
    assert redacted["credentials"]["api_key"] == REDACTION_MASK
    assert entries[0].reason == "allowlist"
    assert entries[1].reason == "masked"


def test_apply_redaction_masks_field_absent_is_recorded():
    _redacted, entries = apply_redaction_masks(
        {"body": "x"},
        [FieldRedactionPolicy(path="credentials.api_key")],
    )
    assert entries[0].reason == "field-absent"
    assert not entries[0].applied


# ---------------------------------------------------------------------------
# Detection / engine contract
# ---------------------------------------------------------------------------


def test_engine_rejects_guardrail_misrouting():
    """Guardrails must never route through EvalEngine.evaluate."""
    engine = EvalEngine()
    eval_def = _guardrail(name="never-here", action="block")
    with pytest.raises(GuardrailMisroutedError):
        engine.evaluate({"body": "SECRET_ABC12345"}, eval_def)


def test_guardrail_config_rejects_retry_failure_behaviour():
    eval_def = _guardrail(failure_behaviour="warn")
    # Pydantic rejects failure_behaviour='retry' at construction (Literal
    # ['warn','block']), so the engine-level guard must be exercised by
    # bypassing the model (AGENTS.md eval-engine lesson).
    object.__setattr__(eval_def, "failure_behaviour", "retry")
    with pytest.raises(GuardrailConfigError):
        evaluate_guardrails(EvalEngine(), [eval_def], {"body": "clean text"})


def test_guardrail_config_requires_deterministic_detection():
    bad = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="llm-detection-forbidden",
        eval_type=EvalType.GUARDRAIL,
        config={"action": "block", "type": "llm_judge"},
        failure_behaviour="block",
    )
    with pytest.raises(GuardrailConfigError):
        evaluate_guardrails(EvalEngine(), [bad], {})


def test_guardrail_top_level_forbidden_type_with_schema_fails_closed():
    # A top-level ``type`` outside regex|json_schema must NOT be silently
    # downgraded to json_schema merely because a ``schema`` dict is present —
    # the module's fail-closed rule applies to any DECLARED detection type.
    bad = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="llm-with-schema",
        eval_type=EvalType.GUARDRAIL,
        config={"action": "block", "type": "llm_judge", "schema": {"type": "object"}},
        failure_behaviour="block",
    )
    with pytest.raises(GuardrailConfigError):
        evaluate_guardrails(EvalEngine(), [bad], {})


def test_guardrail_regex_requires_pattern_and_field_fail_closed():
    # A block guardrail whose detector cannot run (regex without pattern/field)
    # must fail closed at validation, never silently pass through.
    bad = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="regex-missing-pattern",
        eval_type=EvalType.GUARDRAIL,
        config={"action": "block", "type": "regex"},
        failure_behaviour="block",
    )
    with pytest.raises(GuardrailConfigError):
        evaluate_guardrails(EvalEngine(), [bad], {"body": "anything"})


def test_guardrail_detection_envelope_form_blocks():
    # PRD §8.17 documents the ``detection`` envelope as a valid declaration
    # form. It must be resolved during evaluation — a block guardrail declared
    # with a nested envelope must actually block on a violating payload.
    envelope = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="envelope-block",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "interception_point": "input",
            "detection": {"type": "regex", "field": "body", "pattern": r"SECRET_[A-Z0-9]{8}"},
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(engine, [envelope], {"body": "leak SECRET_ABC12345"})
    results = evaluate_guardrails(engine, [envelope], {"body": "clean text"})
    assert results[0].passed is False  # raw eval: regex did not match


def test_guardrail_detection_envelope_json_schema_blocks():
    envelope = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="envelope-schema",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "interception_point": "input",
            "detection": {
                "type": "json_schema",
                "field": "body",
                "schema": {"type": "object", "required": ["safe"], "properties": {"safe": {"type": "boolean"}}},
            },
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(engine, [envelope], {"body": {"safe": "not-a-bool"}})
    results = evaluate_guardrails(engine, [envelope], {"body": {"safe": True}})
    assert results[0].passed is True


def test_guardrail_nested_regex_field_blocks_on_violating_nested_payload():
    """MAJOR-2: a guardrail detection ``field`` may be a nested static path
    (``config.credentials.api_key``) — it must resolve against the payload and
    actually block on a violating nested value. A top-level-only lookup would
    silently never fire (fail-open)."""
    gr = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="nested-credential",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "interception_point": "input",
            "type": "regex",
            "field": "config.credentials.api_key",
            "pattern": r"sk-[a-z]+-\d{6}",
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(
            engine,
            [gr],
            {"config": {"credentials": {"api_key": "sk-live-123456"}}, "body": "clean"},
        )
    # A clean nested value does not fire the guardrail.
    results = evaluate_guardrails(
        engine,
        [gr],
        {"config": {"credentials": {"api_key": "not-a-secret"}}, "body": "clean"},
    )
    assert results[0].passed is False  # raw eval: regex did not match


def test_guardrail_nested_json_schema_field_blocks_on_violating_nested_payload():
    """MAJOR-2: a json_schema guardrail with a nested detection field resolves
    the dotted path and blocks when the nested payload violates the schema."""
    gr = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="nested-schema",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "interception_point": "input",
            "type": "json_schema",
            "field": "config.credentials.api_key",
            "schema": {"type": "string", "pattern": r"^(?!sk-).+$"},
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(
            engine,
            [gr],
            {"config": {"credentials": {"api_key": "sk-live-123456"}}, "body": "clean"},
        )
    results = evaluate_guardrails(
        engine,
        [gr],
        {"config": {"credentials": {"api_key": "not-a-secret"}}, "body": "clean"},
    )
    assert results[0].passed is True


def test_guardrail_nested_field_in_detection_envelope_blocks():
    """MAJOR-2: a nested detection field inside the ``detection`` envelope form
    resolves the same way (envelope merge + nested resolution compose)."""
    gr = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="envelope-nested",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "interception_point": "input",
            "detection": {
                "type": "regex",
                "field": "config.credentials.api_key",
                "pattern": r"sk-[a-z]+-\d{6}",
            },
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(
            engine,
            [gr],
            {"config": {"credentials": {"api_key": "sk-live-123456"}}, "body": "clean"},
        )


def test_guardrail_detection_envelope_forbidden_type_fails_closed():
    # An envelope that declares a detection type outside the allowed set is
    # rejected — never silently downgraded to another detector.
    envelope = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="envelope-llm",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "interception_point": "input",
            "detection": {"type": "llm_judge", "field": "body", "pattern": r"SECRET_[A-Z0-9]{8}"},
        },
        failure_behaviour="block",
    )
    with pytest.raises(GuardrailConfigError):
        evaluate_guardrails(EvalEngine(), [envelope], {"body": "leak SECRET_ABC12345"})


def test_evaluate_guardrails_block_action_raises_on_failure():
    eval_def = _guardrail(name="no-secrets", action="block", failure_behaviour="block")
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(engine, [eval_def], {"body": "leak SECRET_ABC12345"})
    # A clean payload passes without raising (no regex match → no violation).
    results = evaluate_guardrails(engine, [eval_def], {"body": "clean text"})
    assert results[0].passed is False  # raw eval: regex did not match


def test_evaluate_guardrails_warn_never_raises():
    eval_def = _guardrail(name="advisory", action="warn", failure_behaviour="warn")
    results = evaluate_guardrails(EvalEngine(), [eval_def], {"body": "leak SECRET_ABC12345"})
    assert results[0].passed is True  # raw eval: regex matched the violation


def test_guardrail_json_schema_detection_violation_is_validation_failure():
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="schema-guard",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "type": "json_schema",
            "field": "body",
            "schema": {"type": "object", "required": ["safe"], "properties": {"safe": {"type": "boolean"}}},
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(engine, [eval_def], {"body": {"safe": "not-a-bool"}})
    results = evaluate_guardrails(engine, [eval_def], {"body": {"safe": True}})
    assert results[0].passed is True


def test_guardrail_json_schema_detail_is_value_free():
    # jsonschema's ValidationError.message embeds the raw offending value
    # ('SECRET_ABC12345' is not of type 'boolean'). Guardrail detail is
    # count-only / pattern-descriptive — NEVER raw payload — so the detail
    # must be sanitised to a value-free descriptor even when the block fires.
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="schema-guard",
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": "block",
            "type": "json_schema",
            "field": "body",
            "schema": {"type": "object", "required": ["safe"], "properties": {"safe": {"type": "boolean"}}},
        },
        failure_behaviour="block",
    )
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError) as exc_info:
        evaluate_guardrails(engine, [eval_def], {"body": {"safe": "SECRET_ABC12345"}})
    assert "SECRET_ABC12345" not in str(exc_info.value)
    assert "json_schema validation failed" in str(exc_info.value)
    # The persisted eval_results.detail path is the non-raising variant: assert
    # the sanitised result detail directly.
    warn_def = eval_def.model_copy(update={"config": {**eval_def.config, "action": "warn"}})
    results = evaluate_guardrails(engine, [warn_def], {"body": {"safe": "SECRET_ABC12345"}})
    assert results[0].passed is False
    assert "SECRET_ABC12345" not in results[0].detail
    assert "json_schema validation failed" in results[0].detail


# ---------------------------------------------------------------------------
# Two-phase pass
# ---------------------------------------------------------------------------


def test_run_guardrail_pass_two_phase_detection_then_redaction():
    block = _guardrail(name="must-clean", action="block", failure_behaviour="block")
    redact = _guardrail(
        name="redact-key",
        action="redact",
        failure_behaviour="warn",
        redaction=[{"path": "credentials.api_key", "mode": "transform"}],
    )
    payload = {"credentials": {"api_key": "sk-live-123"}, "body": "clean"}
    outcome = run_guardrail_pass(EvalEngine(), [block, redact], payload)
    # Phase 1 detected a clean body (no regex match → no violation → no block).
    # Phase 2 masked the static path deterministically.
    assert outcome.redactions
    assert outcome.redactions[0].applied


def test_run_guardrail_pass_zero_guardrail_fast_path():
    outcome = run_guardrail_pass(EvalEngine(), [], {"body": "x"})
    assert not outcome.results
    assert not outcome.redactions


def test_run_guardrail_pass_block_fires_before_any_mask():
    block = _guardrail(name="hard-stop", action="block", failure_behaviour="block")
    redact = _guardrail(
        name="never-applied",
        action="redact",
        failure_behaviour="warn",
        redaction=[{"path": "body", "mode": "transform"}],
    )
    with pytest.raises(GuardrailBlockedError):
        run_guardrail_pass(EvalEngine(), [block, redact], {"body": "leak SECRET_ABC12345"})


def test_run_interception_pass_multiple_redact_guardrails_accumulate_entries():
    # Each redact-action guardrail contributes its own RedactionEntry batch;
    # the audit list must accumulate ALL of them, not just the last one's.
    first = _guardrail(
        name="redact-a",
        action="redact",
        failure_behaviour="warn",
        redaction=[{"path": "credentials.api_key", "mode": "transform"}],
    )
    second = _guardrail(
        name="redact-b",
        action="redact",
        failure_behaviour="warn",
        redaction=[{"path": "body", "mode": "transform"}],
    )
    payload = {"credentials": {"api_key": "sk-live-123"}, "body": "clean text"}
    outcome = run_interception_pass(EvalEngine(), [first, second], payload)
    assert len(outcome.redactions) == 2
    assert [e.path for e in outcome.redactions] == ["credentials.api_key", "body"]
    assert all(e.applied for e in outcome.redactions)
    assert outcome.payload["credentials"]["api_key"] == REDACTION_MASK
    assert outcome.payload["body"] == REDACTION_MASK


# ---------------------------------------------------------------------------
# Conformance (three-state for block-action guardrails)
# ---------------------------------------------------------------------------


def test_conformance_present_when_all_confirmed():
    d = derive_conformance_state(["github.read"], {"github.read": True})
    assert d.state == "present"
    assert d.claimed


def test_conformance_absent_when_any_confirmed_missing():
    d = derive_conformance_state(["github.read", "github.write"], {"github.read": True, "github.write": False})
    assert d.state == "absent"
    assert d.missing == ("github.write",)


def test_conformance_unknown_fail_closed_when_unreadable():
    d = derive_conformance_state(["github.read"], {"github.read": None})
    assert d.state == "unknown"
    assert d.unreadable == ("github.read",)


def test_conformance_empty_required_is_no_claim():
    d = derive_conformance_state([], {})
    assert not d.claimed


def test_conformance_block_action_fail_closed_absent_and_unknown():
    # Enforcement contract: absent AND unknown both block for block-action.
    absent = derive_conformance_state(["cap"], {"cap": False})
    unknown = derive_conformance_state(["cap"], {"cap": None})
    assert absent.state in ("absent", "unknown")
    assert unknown.state in ("absent", "unknown")
