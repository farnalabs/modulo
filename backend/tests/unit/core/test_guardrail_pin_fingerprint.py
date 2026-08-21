"""Unit tests for ``fingerprint_guardrail_pins`` (FAR-309 PR B).

The fingerprint is the run-start snapshot-integrity guard: a deterministic
SHA-256 over the canonical JSON of a snapshot's serialized guardrail pin set.
It must be stable (same pins -> same digest), order-independent, and change
when any pin field changes, so the replay seam can detect a tampered or
drifted pin set and fail closed.
"""

import uuid

from modulo.core.guardrails import fingerprint_guardrail_pins

_PIN_A = {
    "id": str(uuid.uuid4()),
    "org_id": str(uuid.uuid4()),
    "pipeline_id": str(uuid.uuid4()),
    "node_id": None,
    "name": "no-secrets",
    "eval_type": "guardrail",
    "config_json": {"action": "block", "type": "regex", "field": "body", "pattern": r"SECRET_[A-Z0-9]{8}"},
    "failure_behaviour": "warn",
    "pass_threshold": None,
    "suite_id": None,
}

_PIN_B = {
    "id": str(uuid.uuid4()),
    "org_id": str(uuid.uuid4()),
    "pipeline_id": str(uuid.uuid4()),
    "node_id": None,
    "name": "redact-keys",
    "eval_type": "guardrail",
    "config_json": {
        "action": "redact",
        "type": "regex",
        "field": "credentials.api_key",
        "pattern": r"sk-[A-Za-z0-9]{24}",
    },
    "failure_behaviour": "warn",
    "pass_threshold": None,
    "suite_id": None,
}


def test_fingerprint_is_deterministic() -> None:
    assert fingerprint_guardrail_pins([_PIN_A, _PIN_B]) == fingerprint_guardrail_pins([_PIN_A, _PIN_B])


def test_fingerprint_is_order_independent() -> None:
    assert fingerprint_guardrail_pins([_PIN_A, _PIN_B]) == fingerprint_guardrail_pins([_PIN_B, _PIN_A])


def test_fingerprint_is_sha256_hex() -> None:
    digest = fingerprint_guardrail_pins([_PIN_A])
    assert isinstance(digest, str)
    assert len(digest) == 64
    int(digest, 16)  # hex


def test_fingerprint_changes_when_a_pin_field_changes() -> None:
    tampered = {**_PIN_A, "config_json": {**_PIN_A["config_json"], "pattern": r"SILENT_[A-Z0-9]{8}"}}
    assert fingerprint_guardrail_pins([_PIN_A]) != fingerprint_guardrail_pins([tampered])


def test_fingerprint_changes_when_a_pin_is_removed() -> None:
    assert fingerprint_guardrail_pins([_PIN_A, _PIN_B]) != fingerprint_guardrail_pins([_PIN_A])


def test_fingerprint_changes_when_a_pin_is_added() -> None:
    assert fingerprint_guardrail_pins([_PIN_A]) != fingerprint_guardrail_pins([_PIN_A, _PIN_B])


def test_fingerprint_none_for_empty_or_none() -> None:
    assert fingerprint_guardrail_pins(None) is None
    assert fingerprint_guardrail_pins([]) is None


def test_fingerprint_filters_non_mapping_entries() -> None:
    """Non-dict entries in the stored list are ignored (they are also skipped
    by the pin rebuild loop) so the digest stays consistent with what is
    actually evaluated."""
    assert fingerprint_guardrail_pins([_PIN_A, "garbage"]) == fingerprint_guardrail_pins([_PIN_A])


def test_fingerprint_changes_when_name_changes() -> None:
    renamed = {**_PIN_A, "name": "no-secrets-v2"}
    assert fingerprint_guardrail_pins([_PIN_A]) != fingerprint_guardrail_pins([renamed])
