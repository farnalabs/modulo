"""Unit tests for the feedback-manager status helpers.

These are the pure helpers extracted out of the monolithic
``feedback_manager`` module: the taint-scrubbing handler-type label, the
retry prior-state stripper and the embedded-correction config readers.
"""

from typing import Any

import pytest

from modulo.core.feedback_manager.status import (
    CORRECTION_TERMINAL_STATUSES,
    VALID_STATUS_TRANSITIONS,
    correction_guardrail_from,
    guardrail_correction_config,
    handler_type_label,
    prior_states_for_retry,
)


class _Guardrail:
    def __init__(self, config: Any) -> None:
        self.config = config


# ---------------------------------------------------------------------------
# handler_type_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_type",
    ["human", "ai_correction", "ai_correction_with_human_review"],
)
def test_known_handler_types_map_to_their_own_literal(handler_type: str) -> None:
    assert handler_type_label(handler_type) == handler_type


@pytest.mark.parametrize(
    "handler_type",
    ["", "HUMAN", "ai", "robot", "human\nINJECTED log line"],
)
def test_unknown_handler_types_collapse_to_unknown(handler_type: str) -> None:
    """The label must never echo caller input back into logs."""
    assert handler_type_label(handler_type) == "unknown"


# ---------------------------------------------------------------------------
# prior_states_for_retry
# ---------------------------------------------------------------------------


def test_retry_states_strip_input_fingerprint_and_input_metric() -> None:
    states = [
        {
            "input_fingerprint": "in-1",
            "input_violation_metric": 0.4,
            "output_fingerprint": "out-1",
            "output_violation_metric": 0.9,
            "attempt": 1,
        }
    ]

    stripped = prior_states_for_retry(states)

    assert stripped == [
        {
            "output_fingerprint": "out-1",
            "output_violation_metric": 0.9,
            "attempt": 1,
        }
    ]


def test_retry_states_do_not_mutate_the_caller_list() -> None:
    original = {"input_fingerprint": "in-1", "output_fingerprint": "out-1"}
    states = [original]

    prior_states_for_retry(states)

    assert original == {"input_fingerprint": "in-1", "output_fingerprint": "out-1"}


def test_retry_states_tolerate_entries_without_the_stripped_keys() -> None:
    assert prior_states_for_retry([{"attempt": 2}]) == [{"attempt": 2}]
    assert not prior_states_for_retry([])


# ---------------------------------------------------------------------------
# guardrail_correction_config
# ---------------------------------------------------------------------------


def test_correction_config_is_returned_when_present() -> None:
    block = {"max_attempts": 3}

    assert guardrail_correction_config(_Guardrail({"correction": block})) is block


@pytest.mark.parametrize("config", [None, "not-a-dict", 7, ["correction"]])
def test_non_dict_config_yields_no_correction_block(config: Any) -> None:
    assert guardrail_correction_config(_Guardrail(config)) is None


def test_missing_or_non_dict_correction_key_yields_none() -> None:
    assert guardrail_correction_config(_Guardrail({})) is None
    assert guardrail_correction_config(_Guardrail({"correction": None})) is None
    assert guardrail_correction_config(_Guardrail({"correction": "on"})) is None


def test_guardrail_without_a_config_attribute_yields_none() -> None:
    assert guardrail_correction_config(object()) is None


# ---------------------------------------------------------------------------
# correction_guardrail_from
# ---------------------------------------------------------------------------


def test_first_guardrail_declaring_a_correction_block_wins() -> None:
    plain = _Guardrail({})
    block = {"max_attempts": 2}
    correcting = _Guardrail({"correction": block})
    later = _Guardrail({"correction": {"max_attempts": 9}})

    assert correction_guardrail_from([plain, correcting, later]) == (correcting, block)


@pytest.mark.parametrize(
    "guardrails",
    [[], [_Guardrail({})], [_Guardrail(None), _Guardrail({"correction": 1})]],
)
def test_no_correction_block_returns_a_none_pair(guardrails: list[Any]) -> None:
    assert correction_guardrail_from(guardrails) == (None, None)


# ---------------------------------------------------------------------------
# Transition tables
# ---------------------------------------------------------------------------


def test_terminal_statuses_have_no_onward_transitions() -> None:
    assert not VALID_STATUS_TRANSITIONS["resolved"]
    assert not VALID_STATUS_TRANSITIONS["dismissed"]


def test_correction_terminal_statuses_are_never_reenterable_by_correction() -> None:
    assert sorted(CORRECTION_TERMINAL_STATUSES) == ["dismissed", "escalated", "resolved"]
    assert "correcting" not in CORRECTION_TERMINAL_STATUSES
    assert "pending" not in CORRECTION_TERMINAL_STATUSES
