"""Unit tests for modulo.core.guardrails.packs.soc2 — SOC 2 TSC pack content.

Covers the pack's CI readiness (every control mapped to a schema-valid
guardrail), instantiation into a valid GuardrailConfigSet, the gap report
(no unmapped / uninstantiable controls), warn-mode-first rollout (observe/warn
shadow then block promotion, redact preserved), the P4.1 redaction control's
static field paths, per-control schema validity, and YAML round-tripping.
"""

from modulo.core.guardrails import GuardrailAction
from modulo.core.guardrails.config import GuardrailConfigItem, GuardrailConfigSet, validate_config_set
from modulo.core.guardrails.packs.soc2 import (
    CARD_PATTERN,
    EMAIL_PATTERN,
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
    assert_pack_ci_ready(SOC2_PACK)  # must not raise


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
