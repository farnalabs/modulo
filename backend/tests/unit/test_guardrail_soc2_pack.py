"""Unit tests for modulo.core.guardrails.packs.soc2 — SOC 2 TSC pack content.

Covers the pack's CI readiness (every control mapped to a schema-valid
guardrail), instantiation into a valid GuardrailConfigSet, the gap report
(no unmapped / uninstantiable controls), warn-mode-first rollout (observe/warn
shadow then block promotion, redact preserved), the P4.1 redaction control's
static field paths, per-control schema validity, YAML round-tripping, and —
most importantly — the correctness of the detection regexes (each matches its
intended content, does not false-positive on benign content, and is linear /
free of ReDoS-nested quantifiers) exercised through the REAL engine detection
path.
"""

import re
import uuid

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.guardrails import GuardrailAction, evaluate_guardrails
from modulo.core.guardrails.config import (
    GuardrailConfigItem,
    GuardrailConfigSet,
    to_eval_config,
    validate_config_set,
)
from modulo.core.guardrails.packs.soc2 import (
    ANOMALY_PATTERN,
    AWS_ACCESS_KEY_PATTERN,
    CARD_PATTERN,
    EMAIL_PATTERN,
    EXECUTABLE_CONTENT_PATTERN,
    PII_DETECTION_PATTERN,
    PII_REDACTION_PATHS,
    SOC2_PACK,
    SSN_PATTERN,
    build_soc2_pack,
)
from modulo.core.guardrails.policy_pack import (
    assert_pack_ci_ready,
    dump_pack,
    instantiate_pack,
    load_pack,
    pack_rollout_config,
    validate_pack,
)

_EXPECTED_CONTROL_IDS = ["CC6.1", "CC6.6", "CC7.2", "CC8.1", "A1.2", "P4.1"]


def _redact_control_guardrail() -> GuardrailConfigItem:
    by_id = {control.id: control for control in SOC2_PACK.controls}
    guardrail = by_id["P4.1"].guardrail
    assert guardrail is not None
    return guardrail


def test_soc2_pack_is_ci_ready():
    result = assert_pack_ci_ready(SOC2_PACK)  # must not raise
    # The CI gate is a validator: a fully-mapped pack passes without raising
    # AND keeps its no-return contract (returns None).
    assert result is None


def test_soc2_pack_gap_report_shows_all_controls_mapped():
    report = validate_pack(SOC2_PACK)
    assert report.pack_id == "soc2"
    assert report.total == len(SOC2_PACK.controls)
    assert report.mapped == report.total
    assert report.unmapped == 0
    assert report.uninstantiable == 0
    assert report.ci_ready is True
    assert not report.errors


def test_soc2_pack_instantiates_to_valid_config_set():
    config_set = instantiate_pack(SOC2_PACK)
    assert isinstance(config_set, GuardrailConfigSet)
    assert len(config_set.guardrails) == len(SOC2_PACK.controls)
    validate_config_set(config_set)  # the whole set is schema-valid


def test_soc2_pack_every_control_is_mapped():
    for control in SOC2_PACK.controls:
        assert control.mapped is True
        assert control.guardrail is not None


def test_soc2_pack_has_expected_controls():
    ids = [control.id for control in SOC2_PACK.controls]
    assert ids == _EXPECTED_CONTROL_IDS


def test_soc2_pack_guardrail_ids_unique():
    guardrail_ids = [control.guardrail.id for control in SOC2_PACK.controls if control.guardrail is not None]
    assert len(guardrail_ids) == len(set(guardrail_ids))


def test_soc2_pack_build_function_returns_equivalent_pack():
    rebuilt = build_soc2_pack()
    assert [c.id for c in rebuilt.controls] == [c.id for c in SOC2_PACK.controls]
    assert_pack_ci_ready(rebuilt)


def test_each_soc2_control_guardrail_is_schema_valid():
    for control in SOC2_PACK.controls:
        guardrail = control.guardrail
        assert guardrail is not None
        validate_config_set(GuardrailConfigSet(guardrails=[guardrail]))


def test_soc2_pack_warn_rollout_sets_observe_warn_actions():
    config_set = pack_rollout_config(SOC2_PACK, mode="warn")
    by_id = {control.id: control.guardrail for control in SOC2_PACK.controls}
    for control_id, guardrail in by_id.items():
        assert guardrail is not None
        item = next(item for item in config_set.guardrails if item.id == guardrail.id)
        if control_id == "P4.1":
            assert item.action == GuardrailAction.REDACT  # rollout never silences redact
        else:
            assert item.action == GuardrailAction.WARN


def test_soc2_pack_block_rollout_sets_block_actions():
    config_set = pack_rollout_config(SOC2_PACK, mode="block")
    by_id = {control.id: control.guardrail for control in SOC2_PACK.controls}
    for control_id, guardrail in by_id.items():
        assert guardrail is not None
        item = next(item for item in config_set.guardrails if item.id == guardrail.id)
        if control_id == "P4.1":
            assert item.action == GuardrailAction.REDACT  # redact is not a rollout mode
        else:
            assert item.action == GuardrailAction.BLOCK


def test_soc2_pack_observe_rollout_sets_observe_actions():
    config_set = pack_rollout_config(SOC2_PACK, mode="observe")
    for control, item in zip(SOC2_PACK.controls, config_set.guardrails, strict=True):
        if control.id == "P4.1":
            assert item.action == GuardrailAction.REDACT
        else:
            assert item.action == GuardrailAction.OBSERVE


def test_soc2_pack_redact_control_has_static_redaction_paths():
    guardrail = _redact_control_guardrail()
    assert guardrail.action == GuardrailAction.REDACT
    assert len(guardrail.redaction) == len(PII_REDACTION_PATHS)
    paths = [rule.path for rule in guardrail.redaction]
    assert paths == list(PII_REDACTION_PATHS)
    assert all(rule.path.strip() for rule in guardrail.redaction)  # non-empty static paths


def test_soc2_pack_redact_control_detection_is_schema_valid():
    guardrail = _redact_control_guardrail()
    assert guardrail.detection.type == "regex"
    assert guardrail.detection.pattern is not None
    assert SSN_PATTERN in guardrail.detection.pattern
    assert CARD_PATTERN in guardrail.detection.pattern
    assert EMAIL_PATTERN in guardrail.detection.pattern
    assert guardrail.detection.field == "body"


def test_soc2_pack_yaml_round_trip_preserves_semantics():
    dumped = dump_pack(SOC2_PACK)
    reloaded = load_pack(dumped)
    assert reloaded.id == SOC2_PACK.id
    assert reloaded.name == SOC2_PACK.name
    assert reloaded.version == SOC2_PACK.version
    assert [c.id for c in reloaded.controls] == [c.id for c in SOC2_PACK.controls]
    for reloaded_control, control in zip(reloaded.controls, SOC2_PACK.controls, strict=True):
        assert reloaded_control.mapped is True
        assert reloaded_control.guardrail is not None
        assert control.guardrail is not None
        assert reloaded_control.guardrail.id == control.guardrail.id
        assert reloaded_control.guardrail.action == control.guardrail.action
    assert_pack_ci_ready(reloaded)


def test_soc2_pack_canary_actions_match_control_semantics():
    by_id = {control.id: control.guardrail for control in SOC2_PACK.controls}
    assert by_id["CC6.1"].action == GuardrailAction.BLOCK
    assert by_id["CC6.6"].action == GuardrailAction.BLOCK
    assert by_id["CC7.2"].action == GuardrailAction.OBSERVE
    assert by_id["CC8.1"].action == GuardrailAction.WARN
    assert by_id["A1.2"].action == GuardrailAction.WARN
    assert by_id["P4.1"].action == GuardrailAction.REDACT


# ---------------------------------------------------------------------------
# Regex correctness — the pack's security-relevant detection patterns
# ---------------------------------------------------------------------------
#
# These are the most security-critical tests. Each regex must:
#   (1) MATCH its intended content (a real AWS key, a real <script> tag, ...),
#   (2) NOT false-positive on benign content, and
#   (3) be LINEAR — no nested quantifiers (ReDoS), which the engine's
#       ``_RE_NESTED_QUANTIFIER`` guard must not reject.
#


def _assert_linear(pattern: str) -> None:
    """A pack detection regex must be linear (no nested quantifiers / ReDoS)."""
    from modulo.core.eval_engine import _RE_NESTED_QUANTIFIER

    assert not _RE_NESTED_QUANTIFIER.search(pattern), f"{pattern!r} has a nested quantifier (ReDoS risk)"
    # Exercise the pattern on a long input to smoke out catastrophic backtracking.
    re.search(pattern, "x" * 50_000)  # must return promptly, never hang


@pytest.mark.parametrize(
    "pattern",
    [
        AWS_ACCESS_KEY_PATTERN,
        EXECUTABLE_CONTENT_PATTERN,
        ANOMALY_PATTERN,
        SSN_PATTERN,
        CARD_PATTERN,
        EMAIL_PATTERN,
        PII_DETECTION_PATTERN,
    ],
    ids=["cc61", "cc66", "cc72", "ssn", "card", "email", "pii"],
)
def test_soc2_pack_detection_patterns_are_linear(pattern):
    _assert_linear(pattern)


@pytest.mark.parametrize(
    ("pattern", "should_match", "should_not_match"),
    [
        (
            AWS_ACCESS_KEY_PATTERN,
            ["AKIAIOSFODNN7EXAMPLE", "my key is ASIAABCDEFGHIJKLMNOP"],
            ["plain text", "AKIA", "AKIAIOSFODNN7EXAMPLX1", "AKIA123456789012345"],
        ),
        (
            EXECUTABLE_CONTENT_PATTERN,
            ["<script>alert(1)</script>", "javascript:void(0)", "TVpQAAAA", "<script src=x>"],
            ["not executable", "<div>script</div>", "TVPQL"],
        ),
        (
            ANOMALY_PATTERN,
            ["' OR '1'='1", "OR 1=1", "or 1=1", "Or 1 = 1", "SELECT * FROM ../etc", "path/../"],
            ["normal SELECT col FROM t", "or 1=2", ". . /", "score 1=1"],
        ),
        (
            SSN_PATTERN,
            ["123-45-6789"],
            ["123456789", "123-45-67890"],
        ),
        (
            CARD_PATTERN,
            ["4111 1111 1111 1111", "4111111111111111"],
            ["4111 1111 1111 1111x"],
        ),
        (
            EMAIL_PATTERN,
            ["foo@bar.com"],
            ["example.com", "not-an-email"],
        ),
        (
            PII_DETECTION_PATTERN,
            ["123-45-6789", "4111 1111 1111 1111", "foo@bar.com"],
            ["example.com", "123456789"],
        ),
    ],
    ids=["cc61", "cc66", "cc72", "ssn", "card", "email", "pii"],
)
def test_soc2_pack_detection_patterns_match_intent_and_avoid_false_positives(pattern, should_match, should_not_match):
    for positive in should_match:
        assert re.search(pattern, positive), f"{pattern!r} should match {positive!r}"
    for negative in should_not_match:
        assert not re.search(pattern, negative), f"{pattern!r} must not match {negative!r}"


# ---------------------------------------------------------------------------
# Real engine detection path — convert each pack guardrail to an EvalDefinition
# mirror and run it through evaluate_guardrails (the actual guardrail runtime).
# ---------------------------------------------------------------------------


def _eval_definition_for(guardrail: GuardrailConfigItem) -> EvalDefinition:
    """Mirror a pack guardrail item into an engine EvalDefinition via to_eval_config."""
    config = to_eval_config(guardrail)
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name=guardrail.id,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour="warn",
    )


def _guardrail_by_control(control_id: str) -> GuardrailConfigItem:
    guardrail = next(c.guardrail for c in SOC2_PACK.controls if c.id == control_id)
    assert guardrail is not None
    return guardrail


def test_soc2_pack_cc61_aws_key_blocks_through_real_engine():
    """CC6.1's regex must fire as a block through the real guardrail runtime."""
    from modulo.core.guardrails import GuardrailBlockedError

    eval_def = _eval_definition_for(_guardrail_by_control("CC6.1"))
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(engine, [eval_def], {"body": "leaked AKIAIOSFODNN7EXAMPLE in text"})
    results = evaluate_guardrails(engine, [eval_def], {"body": "no credentials here"})
    assert results[0].passed is False  # raw eval: regex did not match


def test_soc2_pack_cc66_executable_content_blocks_through_real_engine():
    from modulo.core.guardrails import GuardrailBlockedError

    eval_def = _eval_definition_for(_guardrail_by_control("CC6.6"))
    engine = EvalEngine()
    with pytest.raises(GuardrailBlockedError):
        evaluate_guardrails(engine, [eval_def], {"body": "user input: <script>alert(1)</script>"})
    results = evaluate_guardrails(engine, [eval_def], {"body": "a plain message"})
    assert results[0].passed is False


def test_soc2_pack_cc72_anomaly_observe_detects_through_real_engine():
    """CC7.2 is observe-mode: a violation is a passed raw eval, never a block."""
    eval_def = _eval_definition_for(_guardrail_by_control("CC7.2"))
    engine = EvalEngine()
    results = evaluate_guardrails(engine, [eval_def], {"body": "select * from t where x OR 1=1"})
    assert results[0].passed is True  # regex matched the SQLi marker (a violation)
    lower = evaluate_guardrails(engine, [eval_def], {"body": "select * from t where x or 1=1"})
    assert lower[0].passed is True  # case-insensitive: lowercase 'or 1=1' is also a marker
    clean = evaluate_guardrails(engine, [eval_def], {"body": "a normal query for column col"})
    assert clean[0].passed is False  # no marker, no violation


def test_soc2_pack_cc81_json_schema_warns_through_real_engine():
    """CC8.1 (json_schema) must detect a malformed (non-object) payload.

    The guardrail declares no ``field``, so json_schema validates the WHOLE
    payload — a non-object document is a violation.
    """
    eval_def = _eval_definition_for(_guardrail_by_control("CC8.1"))
    engine = EvalEngine()
    results = evaluate_guardrails(engine, [eval_def], "not-an-object")
    assert results[0].passed is False  # json_schema validation failed = violation
    ok = evaluate_guardrails(engine, [eval_def], {"safe": True})
    assert ok[0].passed is True


def test_soc2_pack_a12_size_json_schema_warns_through_real_engine():
    """A1.2 (json_schema) must flag a payload that exceeds the bounded size."""
    eval_def = _eval_definition_for(_guardrail_by_control("A1.2"))
    engine = EvalEngine()
    oversized = {f"k{i}": "x" * 200_000 for i in range(2)}
    results = evaluate_guardrails(engine, [eval_def], oversized)
    assert results[0].passed is False  # string value over 100k → violation
    ok = evaluate_guardrails(engine, [eval_def], {"a": "b"})
    assert ok[0].passed is True
