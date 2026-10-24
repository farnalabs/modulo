"""Unit tests for the guardrail ``is_muted`` boundary coercion (FAR-2028).

The mute flag decides whether a guardrail's enforcement action is suppressed.
Only a real boolean may turn enforcement off: an unspecified ``MagicMock`` row
(or any other truthy sentinel) must read as the historical unmuted state, never
as muted, otherwise a test double silently disables a security control.
"""

import uuid
from unittest.mock import MagicMock

from modulo.core.eval_engine import EvalType
from modulo.core.guardrails import (
    serialize_guardrail_pin,
    to_engine_definition,
    to_engine_definition_from_pin,
)


def _row(**overrides):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.organisation_id = uuid.uuid4()
    row.pipeline_id = uuid.uuid4()
    row.node_id = None
    row.name = "guardrail"
    row.eval_type = EvalType.GUARDRAIL
    row.config_json = {"action": "block"}
    row.failure_behaviour = "warn"
    row.pass_threshold = None
    row.suite_id = None
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _pin(**overrides):
    pin = {
        "id": str(uuid.uuid4()),
        "org_id": str(uuid.uuid4()),
        "pipeline_id": str(uuid.uuid4()),
        "node_id": None,
        "name": "guardrail",
        "eval_type": EvalType.GUARDRAIL.value,
        "config_json": {"action": "block"},
        "failure_behaviour": "warn",
        "pass_threshold": None,
        "suite_id": None,
    }
    pin.update(overrides)
    return pin


def test_unspecified_mock_row_is_not_muted():
    """An unspecified MagicMock ``is_muted`` (truthy) must not read as muted."""
    row = _row()
    # Never assigned: MagicMock auto-creates the attribute, whose truthiness is
    # True.  That sentinel must not be interpreted as a real mute.
    assert bool(getattr(row, "is_muted", False)) is True

    assert serialize_guardrail_pin(row)["is_muted"] is False
    assert to_engine_definition(row).is_muted is False


def test_explicit_true_row_stays_muted():
    row = _row(is_muted=True)

    assert serialize_guardrail_pin(row)["is_muted"] is True
    assert to_engine_definition(row).is_muted is True


def test_explicit_false_row_is_unmuted():
    row = _row(is_muted=False)

    assert serialize_guardrail_pin(row)["is_muted"] is False
    assert to_engine_definition(row).is_muted is False


def test_missing_pin_key_defaults_to_unmuted():
    assert to_engine_definition_from_pin(_pin()).is_muted is False


def test_truthy_non_boolean_pin_value_is_not_muted():
    """A malformed/non-boolean pin value must not suppress enforcement."""
    assert to_engine_definition_from_pin(_pin(is_muted="false")).is_muted is False
    assert to_engine_definition_from_pin(_pin(is_muted=1)).is_muted is False


def test_boolean_pin_values_round_trip():
    assert to_engine_definition_from_pin(_pin(is_muted=True)).is_muted is True
    assert to_engine_definition_from_pin(_pin(is_muted=False)).is_muted is False
