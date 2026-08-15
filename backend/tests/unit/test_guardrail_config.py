"""Unit tests for modulo.core.guardrails.config — guardrail config-as-code.

Covers YAML load/dump round-trips, validation (bad detection type, missing
pattern/field, non-dict schema, duplicate ids), content-hash stability (same
config / different YAML layout / different order → same hash; different config
→ different hash), per-guardrail diff add/update/remove, snapshot-pin
serialization, and drift detection against engine definitions.
"""

import uuid

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails import GuardrailConfigError
from modulo.core.guardrails.config import (
    GuardrailConfigSet,
    GuardrailDetection,
    GuardrailPin,
    build_config_set_from_definitions,
    check_guardrail_drift,
    diff_config_sets,
    dump_config_set,
    hash_config_set,
    load_config_set,
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
    """Build engine DTOs from config sets (one row per guardrail, all bound)."""
    definitions: list[EvalDefinition] = []
    for config_set in config_sets:
        for item in config_set.guardrails:
            definitions.append(
                EvalDefinition(
                    id=uuid.uuid4(),
                    org_id=_ORG_ID,
                    name=item.id,
                    eval_type=EvalType.GUARDRAIL,
                    config=to_eval_config(item),
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
    assert diff_config_sets(a, b) == []


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
    validate_config_set(config_set)  # must not raise


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
    assert engine_config["required_capabilities"] == []
