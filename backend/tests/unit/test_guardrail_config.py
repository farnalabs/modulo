"""Unit tests for modulo.core.guardrails.config — guardrail config-as-code.

Covers YAML load/dump round-trips, validation (bad detection type, missing
pattern/field, non-dict schema, duplicate ids), content-hash stability (same
config / different YAML layout / different order → same hash; different config
→ different hash), per-guardrail diff add/update/remove, snapshot-pin
serialization, and drift detection against engine definitions.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails import GuardrailAction, GuardrailConfigError
from modulo.core.guardrails.config import (
    GuardrailConfigSet,
    GuardrailDetection,
    GuardrailPin,
    build_config_set_from_definitions,
    check_guardrail_drift,
    config_item_from_engine_definition,
    diff_config_sets,
    dump_config_set,
    hash_config_set,
    hash_guardrail_item,
    load_config_set,
    mask_config_set,
    to_eval_config,
    validate_config_set,
)

_ORG_ID = uuid.uuid4()

# Regex deny-rule config: a credential-bearing field is present when the
# pattern matches (the guardrail's violation).
_REGEX_YAML = """
version: 1
guardrails:
  - id: no-aws-keys
    name: Block AWS keys
    action: block
    detection:
      type: regex
      pattern: 'AKIA[0-9A-Z]{16}'
      field: body
    redaction:
      - path: body
        mode: transform
"""

_JSON_SCHEMA_YAML = """
version: 1
guardrails:
  - id: valid-payload
    name: Require valid payload
    action: observe
    detection:
      type: json_schema
      schema:
        type: object
        properties:
          body:
            type: string
"""


def _definitions(config_sets: list[GuardrailConfigSet]) -> list[EvalDefinition]:
    """Build engine DTOs from config sets (one row per guardrail, all bound).

    Mirrors the apply path: the org-level knobs are mirrored onto every row's
    ``config_json`` (``to_eval_config``) so a set rebuilt from rows hashes
    identically and drift stays clean.
    """
    definitions: list[EvalDefinition] = []
    for config_set in config_sets:
        for item in config_set.guardrails:
            definitions.append(
                EvalDefinition(
                    id=uuid.uuid4(),
                    org_id=_ORG_ID,
                    name=item.id,
                    eval_type=EvalType.GUARDRAIL,
                    config=to_eval_config(
                        item,
                        max_guardrails_per_node=config_set.max_guardrails_per_node,
                        guardrail_timeout_seconds=config_set.guardrail_timeout_seconds,
                    ),
                    failure_behaviour="warn",
                )
            )
    return definitions


# ---------------------------------------------------------------------------
# YAML load / dump round-trip
# ---------------------------------------------------------------------------


def test_load_config_set_regex():
    config_set = load_config_set(_REGEX_YAML)
    assert config_set.version == 1
    assert len(config_set.guardrails) == 1
    item = config_set.guardrails[0]
    assert item.id == "no-aws-keys"
    assert item.action.value == "block"
    assert item.detection.type == "regex"
    assert item.detection.pattern == "AKIA[0-9A-Z]{16}"
    assert item.detection.field == "body"
    assert item.redaction[0].path == "body"


def test_load_config_set_json_schema():
    config_set = load_config_set(_JSON_SCHEMA_YAML)
    item = config_set.guardrails[0]
    assert item.detection.type == "json_schema"
    assert item.detection.schema_data == {"type": "object", "properties": {"body": {"type": "string"}}}


def test_dump_round_trip_preserves_semantics():
    config_set = load_config_set(_REGEX_YAML)
    dumped = dump_config_set(config_set)
    reloaded = load_config_set(dumped)
    assert hash_config_set(reloaded) == hash_config_set(config_set)


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_bad_detection_type_rejected():
    yaml_text = _REGEX_YAML.replace("type: regex", "type: llm_judge")
    with pytest.raises(GuardrailConfigError):
        load_config_set(yaml_text)


def test_regex_missing_pattern_rejected():
    yaml_text = """
version: 1
guardrails:
  - id: gr
    name: GR
    detection:
      type: regex
      field: body
"""
    with pytest.raises(GuardrailConfigError, match="pattern"):
        load_config_set(yaml_text)


def test_regex_missing_field_rejected():
    yaml_text = """
version: 1
guardrails:
  - id: gr
    name: GR
    detection:
      type: regex
      pattern: 'x'
"""
    with pytest.raises(GuardrailConfigError, match="field"):
        load_config_set(yaml_text)


def test_json_schema_non_dict_rejected():
    yaml_text = """
version: 1
guardrails:
  - id: gr
    name: GR
    detection:
      type: json_schema
      schema: not-a-dict
"""
    with pytest.raises(GuardrailConfigError, match="schema"):
        load_config_set(yaml_text)


def test_duplicate_id_rejected():
    yaml_text = """
version: 1
guardrails:
  - id: dup
    name: One
    detection:
      type: regex
      pattern: 'x'
      field: body
  - id: dup
    name: Two
    detection:
      type: regex
      pattern: 'y'
      field: body
"""
    with pytest.raises(GuardrailConfigError, match="Duplicate"):
        load_config_set(yaml_text)


def test_non_mapping_document_rejected():
    with pytest.raises(GuardrailConfigError):
        load_config_set("- just\n- a\n- list\n")


def test_empty_config_rejected():
    with pytest.raises(GuardrailConfigError):
        load_config_set("")


def test_json_schema_cannot_carry_pattern_field():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        GuardrailDetection(type="json_schema", schema={}, pattern="x")
    with pytest.raises(pydantic.ValidationError):
        GuardrailDetection(type="regex", pattern="x", field="body", schema={})


# ---------------------------------------------------------------------------
# Content hashing — stability
# ---------------------------------------------------------------------------


def test_hash_stable_across_yaml_layout():
    a = load_config_set(_REGEX_YAML)
    reordered = """
guardrails:
  - detection:
      field: body
      pattern: 'AKIA[0-9A-Z]{16}'
      type: regex
    redaction:
      - mode: transform
        path: body
    action: block
    name: Block AWS keys
    id: no-aws-keys
version: 1
"""
    b = load_config_set(reordered)
    assert hash_config_set(a) == hash_config_set(b)


def test_hash_stable_across_guardrail_order():
    first = """
version: 1
guardrails:
  - id: alpha
    name: A
    detection: {type: regex, pattern: 'x', field: body}
  - id: beta
    name: B
    detection: {type: regex, pattern: 'y', field: body}
"""
    second = """
version: 1
guardrails:
  - id: beta
    name: B
    detection: {type: regex, pattern: 'y', field: body}
  - id: alpha
    name: A
    detection: {type: regex, pattern: 'x', field: body}
"""
    assert hash_config_set(load_config_set(first)) == hash_config_set(load_config_set(second))


def test_hash_differs_for_different_config():
    a = load_config_set(_REGEX_YAML)
    changed = _REGEX_YAML.replace("AKIA[0-9A-Z]{16}", "SK-[0-9A-Za-z]{32}")
    b = load_config_set(changed)
    assert hash_config_set(a) != hash_config_set(b)


def test_hash_is_sha256_hex():
    digest = hash_config_set(load_config_set(_REGEX_YAML))
    assert len(digest) == 64
    int(digest, 16)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_diff_add():
    current = GuardrailConfigSet()
    proposed = load_config_set(_REGEX_YAML)
    changes = diff_config_sets(current, proposed)
    assert len(changes) == 1
    change = changes[0]
    assert change.action == "add"
    assert change.id == "no-aws-keys"
    assert change.new_hash is not None


def test_diff_remove():
    current = load_config_set(_REGEX_YAML)
    proposed = GuardrailConfigSet()
    changes = diff_config_sets(current, proposed)
    assert len(changes) == 1
    change = changes[0]
    assert change.action == "remove"
    assert change.id == "no-aws-keys"
    assert change.old_hash is not None


def test_diff_update():
    current = load_config_set(_REGEX_YAML)
    proposed = load_config_set(_REGEX_YAML.replace("block", "warn"))
    changes = diff_config_sets(current, proposed)
    assert len(changes) == 1
    change = changes[0]
    assert change.action == "update"
    assert change.id == "no-aws-keys"
    assert change.old_hash != change.new_hash


def test_diff_empty_for_identical_sets():
    a = load_config_set(_REGEX_YAML)
    b = load_config_set(_REGEX_YAML)
    assert not diff_config_sets(a, b)


# ---------------------------------------------------------------------------
# Snapshot pin
# ---------------------------------------------------------------------------


def test_pin_round_trip():
    pin = GuardrailPin(
        org_id=_ORG_ID,
        applied_hash="a" * 64,
        applied_at="2026-08-15T00:00:00+00:00",
        serialized_snapshot=_REGEX_YAML,
        status="clean",
    )
    restored = GuardrailPin.from_json(_ORG_ID, pin.to_json())
    assert restored is not None
    assert restored.org_id == _ORG_ID
    assert restored.applied_hash == pin.applied_hash
    assert restored.serialized_snapshot == _REGEX_YAML
    assert restored.status == "clean"


def test_pin_from_json_none():
    assert GuardrailPin.from_json(_ORG_ID, None) is None
    assert GuardrailPin.from_json(_ORG_ID, {}) is None


def test_pin_status_fallback():
    pin = GuardrailPin.from_json(_ORG_ID, {"status": "bogus"})
    assert pin is not None
    assert pin.status == "clean"


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_drift_clean_when_rows_match_pin():
    applied = load_config_set(_REGEX_YAML)
    pin = GuardrailPin(org_id=_ORG_ID, applied_hash=hash_config_set(applied), status="clean")
    definitions = _definitions([applied])
    assert check_guardrail_drift(definitions, pin) is False


def test_drift_clean_when_empty_config():
    pin = GuardrailPin(org_id=_ORG_ID, applied_hash=hash_config_set(GuardrailConfigSet()), status="clean")
    assert check_guardrail_drift([], pin) is False


def test_drift_detected_when_row_mutated():
    applied = load_config_set(_REGEX_YAML)
    pin = GuardrailPin(org_id=_ORG_ID, applied_hash=hash_config_set(applied), status="clean")
    mutated = load_config_set(_REGEX_YAML.replace("AKIA[0-9A-Z]{16}", "AKIA[0-9A-Z]{20}"))
    definitions = _definitions([mutated])
    assert check_guardrail_drift(definitions, pin) is True


def test_drift_detected_when_config_missing_from_rows():
    applied = load_config_set(_REGEX_YAML)
    pin = GuardrailPin(org_id=_ORG_ID, applied_hash=hash_config_set(applied), status="clean")
    # Rows exist for a different guardrail id — the applied set is not present.
    definitions = _definitions([load_config_set(_JSON_SCHEMA_YAML)])
    assert check_guardrail_drift(definitions, pin) is True


def test_drift_when_no_pin_but_rows_exist():
    definitions = _definitions([load_config_set(_REGEX_YAML)])
    assert check_guardrail_drift(definitions, None) is True


def test_build_config_set_dedupes_replicated_rows():
    applied = load_config_set(_REGEX_YAML)
    # Same org-level guardrail replicated across two pipelines → deduped to one.
    definitions = _definitions([applied, applied])
    rebuilt = build_config_set_from_definitions(definitions)
    assert len(rebuilt.guardrails) == 1
    assert hash_config_set(rebuilt) == hash_config_set(applied)


def test_validate_config_set_accepts_valid_set():
    config_set = load_config_set(_REGEX_YAML)
    assert validate_config_set(config_set) is None  # must not raise


def test_config_round_trip_to_eval_config():
    config_set = load_config_set(_REGEX_YAML)
    item = config_set.guardrails[0]
    engine_config = to_eval_config(item)
    assert engine_config["interception_point"] == "input"
    assert engine_config["action"] == "block"
    assert engine_config["type"] == "regex"
    assert engine_config["pattern"] == "AKIA[0-9A-Z]{16}"
    assert engine_config["field"] == "body"
    assert engine_config["redaction"] == [{"path": "body", "mode": "transform"}]
    assert not engine_config["required_capabilities"]


# ---------------------------------------------------------------------------
# Engine-definition rebuild — detection envelope round-trip (PRD §8.17)
# ---------------------------------------------------------------------------


def _envelope_definition(name: str, config: dict[str, Any]) -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour="warn",
    )


def test_config_item_null_redaction_path_behaves_like_missing_key():
    """gh-1802: an explicit ``None`` redaction path must rebuild exactly like a
    missing key — empty string, which fails ``RedactionRule`` ``min_length=1``
    validation — never the literal string "None"."""
    common = {
        "interception_point": "input",
        "action": "block",
        "detection": {"type": "regex", "pattern": "AKIA[0-9A-Z]{16}", "field": "body"},
    }
    with pytest.raises(ValidationError):
        config_item_from_engine_definition(
            _envelope_definition("no-aws-keys", {**common, "redaction": [{"path": None, "mode": "transform"}]})
        )
    with pytest.raises(ValidationError):
        config_item_from_engine_definition(
            _envelope_definition("no-aws-keys", {**common, "redaction": [{"mode": "transform"}]})
        )


def test_config_item_rebuilt_from_envelope_regex_is_complete():
    """A row authored with the documented ``detection`` envelope must rebuild
    with pattern + field intact — the envelope is authoritative and merges into
    the effective config, so the rebuilt item is not a lossy representation."""
    envelope_def = _envelope_definition(
        "no-aws-keys",
        {
            "interception_point": "input",
            "action": "block",
            "redaction": [{"path": "body", "mode": "transform"}],
            "detection": {"type": "regex", "pattern": "AKIA[0-9A-Z]{16}", "field": "body"},
        },
    )
    rebuilt = config_item_from_engine_definition(envelope_def)
    assert rebuilt.id == "no-aws-keys"
    assert rebuilt.action.value == "block"
    assert rebuilt.detection.type == "regex"
    assert rebuilt.detection.pattern == "AKIA[0-9A-Z]{16}"
    assert rebuilt.detection.field == "body"
    assert rebuilt.redaction[0].path == "body"

    # The rebuilt item hashes identically to the equivalent config-as-code
    # item — a faithful round-trip, so drift stays clean.
    applied_item = load_config_set(_REGEX_YAML).guardrails[0]
    assert hash_guardrail_item(rebuilt) == hash_guardrail_item(applied_item)


def test_config_item_rebuilt_from_envelope_json_schema_is_complete():
    schema = {"type": "object", "properties": {"body": {"type": "string"}}}
    envelope_def = _envelope_definition(
        "valid-payload",
        {
            "interception_point": "input",
            "action": "observe",
            "detection": {"type": "json_schema", "schema": schema},
        },
    )
    rebuilt = config_item_from_engine_definition(envelope_def)
    assert rebuilt.detection.type == "json_schema"
    assert rebuilt.detection.schema_data == schema

    applied_item = load_config_set(_JSON_SCHEMA_YAML).guardrails[0]
    assert hash_guardrail_item(rebuilt) == hash_guardrail_item(applied_item)


def test_config_item_rebuilt_from_flattened_row_is_unchanged():
    """The flattened form config-as-code writes must still round-trip
    identically through the engine resolver (backwards compatibility)."""
    applied_item = load_config_set(_REGEX_YAML).guardrails[0]
    flattened_def = _envelope_definition("no-aws-keys", to_eval_config(applied_item))
    rebuilt = config_item_from_engine_definition(flattened_def)
    assert hash_guardrail_item(rebuilt) == hash_guardrail_item(applied_item)


def test_config_item_rebuild_does_not_downgrade_unknown_type():
    """An unknown declared detection type must surface loudly (fail closed),
    never be silently downgraded to regex as the old duplicated resolver did."""
    unknown_def = _envelope_definition(
        "bad-type",
        {"interception_point": "input", "action": "observe", "type": "llm_judge"},
    )
    with pytest.raises(GuardrailConfigError, match="regex or json_schema"):
        config_item_from_engine_definition(unknown_def)


# ---------------------------------------------------------------------------
# Legacy T1 rows — fail closed, never a bare pydantic.ValidationError
# ---------------------------------------------------------------------------


def test_config_item_rebuild_fails_closed_on_legacy_name_with_spaces():
    """The shipped T1 engine allows org-level guardrail names the config id
    pattern rejects (here spaces in ``Block AWS keys``). The conversion must
    raise GuardrailConfigError with the offending name — not a bare
    pydantic.ValidationError that the read surface would map to a generic 422,
    and not a silent skip that would corrupt the rebuild hash."""
    legacy_def = _envelope_definition(
        "Block AWS keys",
        {"interception_point": "input", "action": "observe", "type": "regex", "pattern": "AKIA", "field": "body"},
    )
    with pytest.raises(GuardrailConfigError, match="Block AWS keys"):
        config_item_from_engine_definition(legacy_def)


def test_config_item_rebuild_fails_closed_on_overlong_legacy_name():
    legacy_def = _envelope_definition(
        "a" * 150,
        {"interception_point": "input", "action": "observe", "type": "regex", "pattern": "AKIA", "field": "body"},
    )
    with pytest.raises(GuardrailConfigError, match="cannot be represented"):
        config_item_from_engine_definition(legacy_def)


def test_build_config_set_fails_closed_on_legacy_name():
    """One non-conforming legacy row must fail the whole set build, never be
    silently dropped from the rebuilt config (which would change the hash and
    hide the drift)."""
    definitions = _definitions([load_config_set(_REGEX_YAML)])
    definitions.append(
        _envelope_definition(
            "Block AWS keys",
            {"interception_point": "input", "action": "observe", "type": "regex", "pattern": "AKIA", "field": "body"},
        )
    )
    with pytest.raises(GuardrailConfigError, match="Block AWS keys"):
        build_config_set_from_definitions(definitions)


# ---------------------------------------------------------------------------
# Action resolution — fail closed on unknown, engine default on absent
# ---------------------------------------------------------------------------


def test_config_item_rebuild_fails_closed_on_unknown_action():
    """An unknown/mutated action must fail closed (never silently downgrade to
    OBSERVE), mirroring the detection-type fail-closed path — a downgrade would
    mask a drifted action in the rebuild hash."""
    unknown_def = _envelope_definition(
        "gr-unknown-action",
        {"interception_point": "input", "action": "silent_block", "type": "regex", "pattern": "x", "field": "body"},
    )
    with pytest.raises(GuardrailConfigError, match="silent_block"):
        config_item_from_engine_definition(unknown_def)


def test_config_item_rebuild_preserves_absent_action_as_observe():
    """A row with no action key is the engine's default (observe) — preserving
    it is not a downgrade, so it must not fail closed."""
    no_action_def = _envelope_definition(
        "gr-no-action",
        {"interception_point": "input", "type": "regex", "pattern": "x", "field": "body"},
    )
    rebuilt = config_item_from_engine_definition(no_action_def)
    assert rebuilt.action == GuardrailAction.OBSERVE


# ---------------------------------------------------------------------------
# FAR-223 item 7 — org-level set knobs (max_guardrails_per_node, timeout)
# ---------------------------------------------------------------------------


def test_config_set_default_knobs():
    config_set = GuardrailConfigSet()
    assert config_set.max_guardrails_per_node == 8
    assert config_set.guardrail_timeout_seconds == 2.0


def test_config_set_knobs_round_trip_through_yaml():
    yaml_text = """
version: 1
max_guardrails_per_node: 4
guardrail_timeout_seconds: 1.5
guardrails:
  - id: no-aws-keys
    name: Block AWS keys
    action: block
    detection:
      type: regex
      pattern: 'AKIA[0-9A-Z]{16}'
      field: body
"""
    config_set = load_config_set(yaml_text)
    assert config_set.max_guardrails_per_node == 4
    assert config_set.guardrail_timeout_seconds == 1.5


def test_config_set_knobs_rejected_when_invalid():
    with pytest.raises(GuardrailConfigError):
        load_config_set("version: 1\nmax_guardrails_per_node: -1\nguardrails: []")
    with pytest.raises(GuardrailConfigError):
        load_config_set("version: 1\nguardrail_timeout_seconds: 0\nguardrails: []")


def test_to_eval_config_mirrors_org_knobs_onto_rows():
    config_set = load_config_set(
        "version: 1\nmax_guardrails_per_node: 4\nguardrail_timeout_seconds: 1.5\n"
        + "guardrails:\n  - id: no-aws-keys\n    name: Block\n    action: block\n"
        + "    detection:\n      type: regex\n      pattern: 'AKIA[0-9A-Z]{16}'\n      field: body\n"
    )
    item = config_set.guardrails[0]
    engine_config = to_eval_config(
        item,
        max_guardrails_per_node=config_set.max_guardrails_per_node,
        guardrail_timeout_seconds=config_set.guardrail_timeout_seconds,
    )
    assert engine_config["max_guardrails_per_node"] == 4
    assert engine_config["guardrail_timeout_seconds"] == 1.5


def test_knobs_do_not_drift_on_rebuild_from_rows():
    """The org knobs are mirrored onto every row, so a set rebuilt from rows
    hashes identically to the applied set — drift stays clean."""
    config_set = load_config_set(
        "version: 1\nmax_guardrails_per_node: 4\nguardrail_timeout_seconds: 1.5\n"
        + "guardrails:\n  - id: no-aws-keys\n    name: Block\n    action: block\n"
        + "    detection:\n      type: regex\n      pattern: 'AKIA[0-9A-Z]{16}'\n      field: body\n"
    )
    definitions = _definitions([config_set])
    rebuilt = build_config_set_from_definitions(definitions)
    assert rebuilt.max_guardrails_per_node == 4
    assert rebuilt.guardrail_timeout_seconds == 1.5
    assert hash_config_set(rebuilt) == hash_config_set(config_set)
    pin = GuardrailPin(org_id=_ORG_ID, applied_hash=hash_config_set(config_set))
    assert check_guardrail_drift(definitions, pin) is False


# ---------------------------------------------------------------------------
# FAR-309 PR A — elevated-read masking (mask_config_set)
# ---------------------------------------------------------------------------


def test_mask_config_set_masks_regex_pattern_and_redaction_paths():
    """The standard (non-admin) read masks the deny-rule internals: the regex
    pattern and every redaction field path are replaced, while the guardrail
    topology (id, name, action, detection type, field) is preserved."""
    config_set = load_config_set(_REGEX_YAML)
    masked = mask_config_set(config_set)
    masked_item = masked.guardrails[0]

    assert masked_item.id == "no-aws-keys"
    assert masked_item.name == "Block AWS keys"
    assert masked_item.action == GuardrailAction.BLOCK
    assert masked_item.detection.type == "regex"
    assert masked_item.detection.field == "body"
    # The sensitive internals are masked, never leaked.
    assert masked_item.detection.pattern == "********"
    assert masked_item.redaction[0].path == "********"
    # The original set is untouched (structural copy).
    assert config_set.guardrails[0].detection.pattern == "AKIA[0-9A-Z]{16}"
    assert config_set.guardrails[0].redaction[0].path == "body"


def test_mask_config_set_masks_json_schema():
    config_set = load_config_set(_JSON_SCHEMA_YAML)
    masked = mask_config_set(config_set)
    masked_item = masked.guardrails[0]

    assert masked_item.id == "valid-payload"
    assert masked_item.detection.type == "json_schema"
    # The JSON schema body is replaced with a redaction marker.
    assert masked_item.detection.schema_data == {"redacted": True}
    assert config_set.guardrails[0].detection.schema_data == {
        "type": "object",
        "properties": {"body": {"type": "string"}},
    }


def test_mask_config_set_preserves_knobs_and_empty_sets():
    empty = mask_config_set(GuardrailConfigSet())
    assert not empty.guardrails
    assert empty.max_guardrails_per_node == GuardrailConfigSet().max_guardrails_per_node

    config_set = load_config_set(
        "version: 1\nmax_guardrails_per_node: 4\nguardrail_timeout_seconds: 1.5\n"
        + "guardrails:\n  - id: no-aws-keys\n    name: Block\n    action: block\n"
        + "    detection:\n      type: regex\n      pattern: 'AKIA[0-9A-Z]{16}'\n      field: body\n"
    )
    masked = mask_config_set(config_set)
    assert masked.max_guardrails_per_node == 4
    assert masked.guardrail_timeout_seconds == 1.5
    assert masked.guardrails[0].detection.field == "body"


def test_mask_config_set_never_returns_a_masked_pattern_in_yaml_dump():
    """The masked set's YAML dump must never contain the real pattern/schema —
    the standard read path dumps ``mask_config_set(...)`` output."""
    config_set = load_config_set(_REGEX_YAML)
    masked_yaml = dump_config_set(mask_config_set(config_set))
    assert "AKIA[0-9A-Z]{16}" not in masked_yaml
    assert "********" in masked_yaml
