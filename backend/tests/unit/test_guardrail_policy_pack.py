"""Unit tests for modulo.core.guardrails.policy_pack — compliance policy packs.

Covers the pack schema (valid/invalid controls, mapped flag sync, YAML
load/dump round-trip), instantiation (mapped controls → valid
GuardrailConfigSet, unmapped excluded, uninstantiable reported), the gap
report (mapped/unmapped/uninstantiable counts + errors), the CI gate (a pack
with a gap raises, a complete pack passes), and warn-mode-first rollout
(observe/warn shadow modes, block promotion, invalid modes rejected).
"""

import pydantic
import pytest

from modulo.core.guardrails import GuardrailAction, GuardrailConfigError
from modulo.core.guardrails.config import (
    GuardrailConfigItem,
    GuardrailConfigSet,
    GuardrailDetection,
    hash_config_set,
    validate_config_set,
)
from modulo.core.guardrails.policy_pack import (
    PolicyControl,
    PolicyPack,
    assert_pack_ci_ready,
    dump_pack,
    instantiate_pack,
    load_pack,
    load_pack_file,
    main,
    pack_rollout_config,
    validate_pack,
)


def _guardrail(gid: str = "no-aws-keys", action: str = "observe") -> GuardrailConfigItem:
    """A schema-valid regex deny-rule guardrail (pattern + field present)."""
    return GuardrailConfigItem(
        id=gid,
        name="Block AWS keys",
        action=action,
        detection=GuardrailDetection(type="regex", pattern="AKIA[0-9A-Z]{16}", field="body"),
    )


def _json_schema_guardrail(gid: str = "valid-payload") -> GuardrailConfigItem:
    """A schema-valid json_schema guardrail."""
    return GuardrailConfigItem(
        id=gid,
        name="Require valid payload",
        detection=GuardrailDetection(type="json_schema", schema={"type": "object"}),
    )


def _control(
    cid: str = "CC6.1",
    title: str = "Encryption of data",
    guardrail: GuardrailConfigItem | None = None,
) -> PolicyControl:
    return PolicyControl(
        id=cid,
        title=title,
        description="Control description",
        guardrail=guardrail,
        mapped=guardrail is not None,
    )


def _pack(controls: list[PolicyControl] | None = None) -> PolicyPack:
    return PolicyPack(id="soc2", name="SOC 2", version="1.0.0", controls=controls or [_control(guardrail=_guardrail())])


_PACK_YAML = """
id: soc2
name: SOC 2
version: 1.0.0
controls:
  - id: CC6.1
    title: Encryption of data
    description: Data is encrypted at rest and in transit.
    mapped: true
    guardrail:
      id: no-aws-keys
      name: Block AWS keys
      action: observe
      detection:
        type: regex
        pattern: 'AKIA[0-9A-Z]{16}'
        field: body
"""


# ---------------------------------------------------------------------------
# Pack schema
# ---------------------------------------------------------------------------


def test_valid_pack_parses():
    pack = _pack()
    assert pack.id == "soc2"
    assert pack.name == "SOC 2"
    assert pack.version == "1.0.0"
    assert len(pack.controls) == 1
    control = pack.controls[0]
    assert control.id == "CC6.1"
    assert control.mapped
    assert control.guardrail is not None
    assert control.guardrail.detection.type == "regex"


def test_load_pack_parses_yaml():
    pack = load_pack(_PACK_YAML)
    assert pack.id == "soc2"
    assert pack.controls[0].id == "CC6.1"
    assert pack.controls[0].mapped
    assert pack.controls[0].guardrail is not None
    assert pack.controls[0].guardrail.action == GuardrailAction.OBSERVE


def test_dump_pack_round_trip_preserves_semantics():
    pack = load_pack(_PACK_YAML)
    dumped = dump_pack(pack)
    reloaded = load_pack(dumped)
    assert reloaded.id == pack.id
    assert [c.id for c in reloaded.controls] == [c.id for c in pack.controls]
    reloaded_guardrail = reloaded.controls[0].guardrail
    assert reloaded_guardrail is not None
    assert pack.controls[0].guardrail is not None
    assert reloaded_guardrail.id == pack.controls[0].guardrail.id
    assert reloaded_guardrail.detection.pattern == pack.controls[0].guardrail.detection.pattern


def test_invalid_control_id_rejected():
    with pytest.raises(pydantic.ValidationError):
        PolicyControl(id="bad id with space", title="Bad", guardrail=_guardrail(), mapped=True)


def test_duplicate_control_id_rejected():
    control = _control(guardrail=_guardrail())
    with pytest.raises(ValueError, match="Duplicate control id"):
        PolicyPack(
            id="soc2", name="SOC 2", controls=[control, _control(cid="CC6.1", guardrail=_json_schema_guardrail())]
        )


def test_control_marked_mapped_without_guardrail_rejected():
    with pytest.raises(ValueError, match="is marked mapped but has no guardrail mapping"):
        PolicyControl(id="CC6.1", title="Encryption", mapped=True)


def test_control_mapped_flag_syncs_from_guardrail():
    control = _control(guardrail=_guardrail())
    assert control.mapped is True


def test_control_without_guardrail_is_unmapped():
    control = _control()
    assert control.mapped is False
    assert control.guardrail is None


def test_load_pack_rejects_empty():
    with pytest.raises(GuardrailConfigError):
        load_pack("   \n  ")


def test_load_pack_rejects_non_mapping():
    with pytest.raises(GuardrailConfigError):
        load_pack("- just\n- a\n- list\n")


def test_load_pack_rejects_malformed_yaml():
    with pytest.raises(GuardrailConfigError):
        load_pack("id: soc2\ncontrols: [unclosed")


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


def test_instantiate_pack_mapped_controls_build_valid_set():
    pack = _pack(
        [_control(cid="CC6.1", guardrail=_guardrail()), _control(cid="CC7.1", guardrail=_json_schema_guardrail())]
    )
    config_set = instantiate_pack(pack)
    assert isinstance(config_set, GuardrailConfigSet)
    assert len(config_set.guardrails) == 2
    validate_config_set(config_set)  # the whole set is schema-valid
    assert {item.id for item in config_set.guardrails} == {"no-aws-keys", "valid-payload"}


def test_instantiate_pack_excludes_unmapped_controls():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail()),
            _control(cid="CC7.2", title="Unmapped control"),
        ]
    )
    config_set = instantiate_pack(pack)
    assert len(config_set.guardrails) == 1
    assert config_set.guardrails[0].id == "no-aws-keys"


def test_instantiate_pack_uninstantiable_control_raises():
    # A regex guardrail with a pattern but no field fails validate_config_set.
    broken = GuardrailConfigItem(
        id="no-aws-keys",
        name="Broken",
        detection=GuardrailDetection(type="regex", pattern="AKIA[0-9A-Z]{16}"),
    )
    pack = _pack([_control(cid="CC6.1", guardrail=broken)])
    with pytest.raises(GuardrailConfigError):
        instantiate_pack(pack)


def test_instantiate_pack_duplicate_guardrail_id_raises():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail()),
            _control(cid="CC7.1", guardrail=_guardrail(gid="no-aws-keys")),
        ]
    )
    with pytest.raises(GuardrailConfigError):
        instantiate_pack(pack)


# ---------------------------------------------------------------------------
# Gap report
# ---------------------------------------------------------------------------


def test_validate_pack_clean_pack_reports_mapped():
    report = validate_pack(_pack([_control(cid="CC6.1", guardrail=_guardrail())]))
    assert report.pack_id == "soc2"
    assert report.total == 1
    assert report.mapped == 1
    assert report.unmapped == 0
    assert report.uninstantiable == 0
    assert report.ci_ready is True
    assert not report.errors


def test_validate_pack_reports_unmapped_controls():
    report = validate_pack(_pack([_control(cid="CC6.1", guardrail=_guardrail()), _control(cid="CC7.2")]))
    assert report.mapped == 1
    assert report.unmapped == 1
    assert report.total == 2
    assert report.uninstantiable == 0
    assert report.ci_ready is False
    assert report.unmapped_controls == ["CC7.2"]
    assert report.errors == ["control 'CC7.2' is unmapped (no concrete guardrail)"]


def test_validate_pack_reports_uninstantiable_with_error():
    broken = GuardrailConfigItem(
        id="no-aws-keys",
        name="Broken",
        detection=GuardrailDetection(type="regex", pattern="AKIA[0-9A-Z]{16}"),
    )
    report = validate_pack(_pack([_control(cid="CC6.1", guardrail=broken)]))
    assert report.mapped == 1
    assert report.unmapped == 0
    assert report.uninstantiable == 1
    assert report.ci_ready is False
    assert len(report.uninstantiable_controls) == 1
    control_id, error = report.uninstantiable_controls[0]
    assert control_id == "CC6.1"
    assert "pattern" in error
    assert "field" in error
    assert any("uninstantiable" in err for err in report.errors)


def test_validate_pack_reports_duplicate_guardrail_id_as_uninstantiable():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail()),
            _control(cid="CC7.1", guardrail=_guardrail(gid="no-aws-keys")),
        ]
    )
    report = validate_pack(pack)
    assert report.mapped == 2
    assert report.uninstantiable == 1
    assert any("duplicate guardrail id" in err for err in report.errors)


def test_validate_pack_report_to_dict():
    report = validate_pack(_pack([_control(cid="CC7.2")]))
    data = report.to_dict()
    assert data["pack_id"] == "soc2"
    assert data["unmapped"] == 1
    assert data["ci_ready"] is False
    assert data["unmapped_controls"] == ["CC7.2"]


# ---------------------------------------------------------------------------
# CI gate
# ---------------------------------------------------------------------------


def test_assert_pack_ci_ready_raises_on_unmapped_gap():
    pack = _pack([_control(cid="CC6.1", guardrail=_guardrail()), _control(cid="CC7.2")])
    with pytest.raises(GuardrailConfigError):
        assert_pack_ci_ready(pack)


def test_assert_pack_ci_ready_raises_on_uninstantiable_gap():
    broken = GuardrailConfigItem(
        id="no-aws-keys",
        name="Broken",
        detection=GuardrailDetection(type="regex", pattern="AKIA[0-9A-Z]{16}"),
    )
    with pytest.raises(GuardrailConfigError):
        assert_pack_ci_ready(_pack([_control(cid="CC6.1", guardrail=broken)]))


def test_assert_pack_ci_ready_passes_complete_pack():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail()),
            _control(cid="CC7.1", guardrail=_json_schema_guardrail()),
        ]
    )
    result = assert_pack_ci_ready(pack)
    # The CI gate is a validator: a complete pack passes without raising AND
    # keeps its no-return contract (returns None).
    assert result is None


# ---------------------------------------------------------------------------
# Warn-mode-first rollout
# ---------------------------------------------------------------------------


def test_rollout_warn_mode_sets_every_guardrail_to_warn():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail(action="block")),
            _control(cid="CC7.1", guardrail=_json_schema_guardrail()),
        ]
    )
    config_set = pack_rollout_config(pack, mode="warn")
    assert all(item.action == GuardrailAction.WARN for item in config_set.guardrails)


def test_rollout_observe_mode_sets_every_guardrail_to_observe():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail(action="block")),
            _control(cid="CC7.1", guardrail=_json_schema_guardrail()),
        ]
    )
    config_set = pack_rollout_config(pack, mode="observe")
    assert all(item.action == GuardrailAction.OBSERVE for item in config_set.guardrails)


def test_rollout_block_mode_promotes_to_block():
    pack = _pack([_control(cid="CC6.1", guardrail=_guardrail(action="observe"))])
    config_set = pack_rollout_config(pack, mode="block")
    assert all(item.action == GuardrailAction.BLOCK for item in config_set.guardrails)


def test_rollout_default_mode_is_warn():
    config_set = pack_rollout_config(_pack([_control(cid="CC6.1", guardrail=_guardrail())]))
    assert all(item.action == GuardrailAction.WARN for item in config_set.guardrails)


def test_rollout_accepts_guardrail_action_enum():
    config_set = pack_rollout_config(_pack([_control(cid="CC6.1", guardrail=_guardrail())]), mode=GuardrailAction.BLOCK)
    assert all(item.action == GuardrailAction.BLOCK for item in config_set.guardrails)


def test_rollout_preserves_guardrail_identity_across_modes():
    pack = _pack([_control(cid="CC6.1", guardrail=_guardrail())])
    warn_set = pack_rollout_config(pack, mode="warn")
    block_set = pack_rollout_config(pack, mode="block")
    assert [item.id for item in warn_set.guardrails] == [item.id for item in block_set.guardrails]
    # Action is part of the content hash — warn vs block differ, proving the
    # promotion is a real change to the shipped config.
    assert hash_config_set(warn_set) != hash_config_set(block_set)


def test_rollout_invalid_mode_raises():
    with pytest.raises(GuardrailConfigError):
        pack_rollout_config(_pack([_control(cid="CC6.1", guardrail=_guardrail())]), mode="redact")
    with pytest.raises(GuardrailConfigError):
        pack_rollout_config(_pack([_control(cid="CC6.1", guardrail=_guardrail())]), mode="explode")


def test_rollout_excludes_unmapped_controls():
    pack = _pack([_control(cid="CC6.1", guardrail=_guardrail()), _control(cid="CC7.2")])
    config_set = pack_rollout_config(pack, mode="block")
    assert [item.id for item in config_set.guardrails] == ["no-aws-keys"]


@pytest.mark.parametrize("mode", ["observe", "warn", "block"])
def test_rollout_preserves_redact_action_under_every_mode(mode):
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail(gid="redact-key", action="redact")),
            _control(cid="CC7.1", guardrail=_guardrail(gid="no-aws-keys", action="observe")),
        ]
    )
    config_set = pack_rollout_config(pack, mode=mode)
    by_id = {item.id: item for item in config_set.guardrails}
    # A redact-action control keeps REDACT — a rollout never silences it.
    assert by_id["redact-key"].action == GuardrailAction.REDACT
    # The non-redact control is coerced to the rollout mode as usual.
    assert by_id["no-aws-keys"].action == GuardrailAction(mode)


# ---------------------------------------------------------------------------
# load_pack_file — reading a pack YAML from disk
# ---------------------------------------------------------------------------


def test_load_pack_file_reads_yaml_from_disk(tmp_path):
    pack_file = tmp_path / "pack.yaml"
    pack_file.write_text(_PACK_YAML, encoding="utf-8")
    pack = load_pack_file(str(pack_file))
    assert pack.id == "soc2"
    assert pack.controls[0].id == "CC6.1"
    assert pack.controls[0].mapped
    assert pack.controls[0].guardrail is not None


def test_load_pack_file_missing_file_raises(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(GuardrailConfigError):
        load_pack_file(str(missing))


# ---------------------------------------------------------------------------
# Unsafe YAML must be rejected (safe-load only)
# ---------------------------------------------------------------------------


def test_load_pack_rejects_unsafe_yaml_constructor():
    # ``!!python/object`` requires yaml.unsafe_load / a custom loader — the
    # framework must refuse it via safe_load, never construct arbitrary
    # objects from pack content.
    unsafe = "!!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(GuardrailConfigError):
        load_pack(unsafe)


# ---------------------------------------------------------------------------
# CI gate — error message carries the gap details
# ---------------------------------------------------------------------------


def test_assert_pack_ci_ready_error_lists_gaps():
    pack = _pack(
        [
            _control(cid="CC6.1", guardrail=_guardrail()),
            _control(cid="CC7.2"),
        ]
    )
    with pytest.raises(GuardrailConfigError) as exc_info:
        assert_pack_ci_ready(pack)
    message = str(exc_info.value)
    assert "soc2" in message
    assert "1 unmapped control" in message
    assert "CC7.2" in message


# ---------------------------------------------------------------------------
# CLI entry point (CI gate wiring)
# ---------------------------------------------------------------------------


def test_main_cli_returns_zero_for_ready_pack(tmp_path, monkeypatch):
    pack_file = tmp_path / "ready.yaml"
    pack_file.write_text(_PACK_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main([str(pack_file)]) == 0


def test_main_cli_returns_one_for_gapped_pack(tmp_path):
    gapped_yaml = """
id: soc2
name: SOC 2
version: 1.0.0
controls:
  - id: CC7.2
    title: Unmapped control
"""
    pack_file = tmp_path / "gapped.yaml"
    pack_file.write_text(gapped_yaml, encoding="utf-8")
    assert main([str(pack_file)]) == 1
