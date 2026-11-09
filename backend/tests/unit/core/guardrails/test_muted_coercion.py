"""Unit tests for the guardrail ``is_muted`` boundary coercion (FAR-2028).

The mute flag decides whether a guardrail's enforcement action is suppressed.
Only a real boolean may turn enforcement off: an unspecified ``MagicMock`` row
(or any other truthy sentinel) must read as the historical unmuted state, never
as muted, otherwise a test double silently disables a security control.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from modulo.core.eval_engine import EvalType
from modulo.core.guardrails import (
    serialize_guardrail_pin,
    to_engine_definition,
    to_engine_definition_from_pin,
)

NON_BOOLEAN_SENTINELS = [None, 0, 1, "", "false", 1.0, []]


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

    assert serialize_guardrail_pin(row)["is_muted"] is False
    assert to_engine_definition(row).is_muted is False


@pytest.mark.parametrize("value", [True, False])
def test_explicit_boolean_row_value_round_trips(value):
    row = _row(is_muted=value)

    assert serialize_guardrail_pin(row)["is_muted"] is value
    assert to_engine_definition(row).is_muted is value


@pytest.mark.parametrize("value", NON_BOOLEAN_SENTINELS)
def test_non_boolean_row_value_is_not_muted(value):
    """A legacy/malformed row value must not suppress enforcement."""
    row = _row(is_muted=value)

    assert serialize_guardrail_pin(row)["is_muted"] is False
    assert to_engine_definition(row).is_muted is False


def test_missing_pin_key_defaults_to_unmuted():
    assert to_engine_definition_from_pin(_pin()).is_muted is False


@pytest.mark.parametrize("value", NON_BOOLEAN_SENTINELS)
def test_non_boolean_pin_value_is_not_muted(value):
    """A malformed/non-boolean pin value must not suppress enforcement."""
    assert to_engine_definition_from_pin(_pin(is_muted=value)).is_muted is False


@pytest.mark.parametrize("value", [True, False])
def test_boolean_pin_value_round_trips(value):
    assert to_engine_definition_from_pin(_pin(is_muted=value)).is_muted is value
