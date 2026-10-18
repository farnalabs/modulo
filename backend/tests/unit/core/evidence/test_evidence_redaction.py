"""Unit tests for the server-side evidence redaction boundary (FAR-944, §2.2/§2.3).

Covers the two redaction classes -> JSON-key mapping, the
``apply_redaction`` semantics (sentinel replacement at any depth, no
over-redaction, no mutation, non-dict passthrough), and the rule-resolution
precedence (most-specific key_pattern -> ``*`` catch-all -> fail-closed
default).

The default is DELIBERATELY fail-closed (a review deviation from acceptance
criterion 3): only TRUSTED roles (``admin``/``operator``) fail open when no
rule matches; every other role — including the default org role ``runner`` —
redacts BOTH classes. These tests pin that split.

``load_redaction_rules`` is exercised against a fake session that returns the
org's rules directly, and ``resolve_redaction_classes_from`` is exercised as a
pure function over that list — the SQL org filter is the DB's job; these tests
pin the Python precedence logic on top of a rule list.
"""

from __future__ import annotations

import logging
import types
import uuid
from typing import Any

from modulo.core.evidence_redaction import (
    REDACTED_SENTINEL,
    REDACTION_CLASS_FIELDS,
    apply_redaction,
    load_redaction_rules,
    resolve_redaction_classes_from,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_BOTH_CLASSES = frozenset({"sensitive_output", "internal_metadata"})


# ---------------------------------------------------------------------------
# Fakes — a rule row and a session returning it
# ---------------------------------------------------------------------------


def _rule(key_pattern: str, role: str, redacted_fields: list[str]) -> Any:
    return types.SimpleNamespace(key_pattern=key_pattern, role=role, redacted_fields=redacted_fields)


class _FakeScalars:
    def __init__(self, rules: list[Any]) -> None:
        self._rules = rules

    def all(self) -> list[Any]:
        return self._rules


class _FakeResult:
    def __init__(self, rules: list[Any]) -> None:
        self._rules = rules

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rules)


class _FakeSession:
    """Minimal session double returning a fixed rule list."""

    def __init__(self, rules: list[Any]) -> None:
        self._rules = rules

    async def execute(self, statement: Any) -> _FakeResult:
        return _FakeResult(self._rules)


async def _resolve(rules: list[Any], role: str, key: str) -> frozenset[str]:
    """Load *rules* through the fake session, then resolve ``(role, key)``."""
    loaded = await load_redaction_rules(_FakeSession(rules), _ORG_ID)
    return resolve_redaction_classes_from(loaded, role, key)


# ---------------------------------------------------------------------------
# Class -> field mapping
# ---------------------------------------------------------------------------


def test_class_mapping_covers_documented_fields() -> None:
    assert "raw_output" in REDACTION_CLASS_FIELDS["sensitive_output"]
    assert "judge_output" in REDACTION_CLASS_FIELDS["sensitive_output"]
    assert "match_groups" in REDACTION_CLASS_FIELDS["sensitive_output"]
    assert "config" in REDACTION_CLASS_FIELDS["internal_metadata"]
    assert "node_id" in REDACTION_CLASS_FIELDS["internal_metadata"]
    assert "pipeline_internals" in REDACTION_CLASS_FIELDS["internal_metadata"]


async def test_builtin_default_redacts_both_classes_for_viewer() -> None:
    classes = await _resolve([], "viewer", "anything")

    assert classes == _BOTH_CLASSES


# ---------------------------------------------------------------------------
# apply_redaction
# ---------------------------------------------------------------------------


def test_viewer_redacts_sensitive_output_but_keeps_permitted_fields() -> None:
    value = {"raw_output": "secret", "status": "complete", "pass": True, "run_id": "r1"}

    result = apply_redaction(value, frozenset({"sensitive_output"}))

    assert result is not None
    assert result["raw_output"] == REDACTED_SENTINEL
    assert result["status"] == "complete"
    assert result["pass"] is True
    assert result["run_id"] == "r1"


def test_empty_class_set_leaves_value_unchanged_for_admin() -> None:
    value = {"raw_output": "secret", "config": {"threshold": 1}}

    result = apply_redaction(value, frozenset())

    assert result == value


def test_internal_metadata_class_redacts_its_fields_only() -> None:
    value = {"config": {"x": 1}, "node_id": "n1", "snapshot": {"id": "s"}, "status": "ok"}

    result = apply_redaction(value, frozenset({"internal_metadata"}))

    assert result is not None
    assert result["config"] == REDACTED_SENTINEL
    assert result["node_id"] == REDACTED_SENTINEL
    assert result["snapshot"] == REDACTED_SENTINEL
    assert result["status"] == "ok"


def test_both_classes_redact_sensitive_and_internal_keys_together() -> None:
    value = {"raw_output": "x", "reasoning": "y", "config": {"a": 1}, "status": "ok", "run_id": "r"}

    result = apply_redaction(value, _BOTH_CLASSES)

    assert result is not None
    assert result["raw_output"] == REDACTED_SENTINEL
    assert result["reasoning"] == REDACTED_SENTINEL
    assert result["config"] == REDACTED_SENTINEL
    assert result["status"] == "ok"
    assert result["run_id"] == "r"


def test_unknown_field_outside_classes_is_never_redacted() -> None:
    value = {"some_new_field": "visible", "raw_output": "hidden"}

    result = apply_redaction(value, frozenset({"sensitive_output"}))

    assert result is not None
    assert result["some_new_field"] == "visible"
    assert result["raw_output"] == REDACTED_SENTINEL


def test_non_dict_value_is_returned_unchanged() -> None:
    assert apply_redaction(True, _BOTH_CLASSES) is True
    assert apply_redaction(None, _BOTH_CLASSES) is None


def test_apply_redaction_does_not_mutate_the_input_mapping() -> None:
    value = {"raw_output": "secret"}

    apply_redaction(value, frozenset({"sensitive_output"}))

    assert value["raw_output"] == "secret"


def test_nested_class_covered_keys_are_redacted_at_any_depth() -> None:
    # A class-covered key nested in another mapping, or inside a list of
    # mappings, must be replaced — a shallow walk would leak it. The container
    # keys ("outer"/"items"/"meta") are themselves outside both classes, so the
    # redaction must descend rather than replace the whole subtree.
    value = {
        "outer": {"raw_output": "secret", "status": "ok"},
        "items": [{"reasoning": "r"}, {"safe": 1}],
        "meta": {"node_id": "n", "keep": True},
    }

    result = apply_redaction(value, _BOTH_CLASSES)

    assert result["outer"]["raw_output"] == REDACTED_SENTINEL
    assert result["outer"]["status"] == "ok"
    assert result["items"][0]["reasoning"] == REDACTED_SENTINEL
    assert result["items"][1]["safe"] == 1
    assert result["meta"]["node_id"] == REDACTED_SENTINEL
    assert result["meta"]["keep"] is True


def test_nested_redaction_does_not_mutate_the_input() -> None:
    value = {"outer": {"raw_output": "secret"}}

    apply_redaction(value, _BOTH_CLASSES)

    assert value["outer"]["raw_output"] == "secret"


def test_non_mapping_container_scalars_pass_through_unchanged() -> None:
    # A list of scalars carries no field name to classify, so it is returned
    # verbatim (documented behaviour).
    assert apply_redaction(["a", 1, True, None], _BOTH_CLASSES) == ["a", 1, True, None]


def test_unknown_stored_class_is_skipped_and_logged(caplog: Any) -> None:
    value = {"raw_output": "secret"}

    with caplog.at_level(logging.WARNING, logger="modulo.core.evidence_redaction"):
        result = apply_redaction(value, frozenset({"not_a_class"}))

    # The unknown class redacts nothing (a no-op), and the misconfiguration is
    # surfaced rather than silently swallowed.
    assert result["raw_output"] == "secret"
    assert any("unknown_class" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# rule resolution — precedence
# ---------------------------------------------------------------------------


async def test_most_specific_pattern_wins() -> None:
    rules = [
        _rule("*", "viewer", ["internal_metadata"]),
        _rule("llm_judge.*", "viewer", ["sensitive_output"]),
        _rule("llm_judge.grade", "viewer", ["sensitive_output", "internal_metadata"]),
    ]

    classes = await _resolve(rules, "viewer", "llm_judge.grade")

    assert classes == _BOTH_CLASSES


async def test_longer_matching_pattern_wins_over_shorter() -> None:
    rules = [
        _rule("llm_judge.*", "viewer", ["sensitive_output"]),
        _rule("llm_judge.grade.*", "viewer", ["internal_metadata"]),
    ]

    classes = await _resolve(rules, "viewer", "llm_judge.grade.score")

    assert classes == frozenset({"internal_metadata"})


async def test_catch_all_is_used_when_no_specific_pattern_matches() -> None:
    rules = [
        _rule("eval.*", "viewer", ["sensitive_output"]),
        _rule("*", "viewer", ["sensitive_output", "internal_metadata"]),
    ]

    classes = await _resolve(rules, "viewer", "risk.api-call")

    assert classes == _BOTH_CLASSES


async def test_non_matching_pattern_without_catch_all_uses_builtin_default() -> None:
    rules = [_rule("eval.*", "viewer", ["sensitive_output"])]

    classes = await _resolve(rules, "viewer", "other.key")

    assert classes == _BOTH_CLASSES


async def test_catch_all_for_a_different_role_is_not_applied() -> None:
    # A viewer catch-all must not leak to another role; with no rule for that
    # role the fail-closed default applies.
    rules = [_rule("*", "viewer", ["sensitive_output"])]

    classes = await _resolve(rules, "runner", "other.key")

    assert classes == _BOTH_CLASSES


def test_equal_length_patterns_break_ties_deterministically() -> None:
    # Two equal-length patterns that both match the key: the tie-break is
    # lexicographic on the pattern, so the result does not depend on row order.
    rules = [
        _rule("llm_judge.a*", "viewer", ["internal_metadata"]),
        _rule("llm_judge.*a", "viewer", ["sensitive_output"]),
    ]

    classes = resolve_redaction_classes_from(rules, "viewer", "llm_judge.aa")
    assert classes == frozenset({"sensitive_output"})

    reordered = resolve_redaction_classes_from(list(reversed(rules)), "viewer", "llm_judge.aa")
    assert reordered == frozenset({"sensitive_output"})


# ---------------------------------------------------------------------------
# Fail-closed default — trusted vs untrusted roles (review deviation)
# ---------------------------------------------------------------------------


async def test_runner_fails_closed_with_no_matching_rule() -> None:
    # Regression (FAR-944 review): ``runner`` is a valid ``evidence.read``
    # principal AND the default org role. Before the fix it resolved to an
    # EMPTY class set and saw every sensitive field — a fail-open hole.
    classes = await _resolve([], "runner", "anything")

    assert classes == _BOTH_CLASSES


async def test_unknown_future_role_fails_closed() -> None:
    # Any role not explicitly trusted redacts both classes, so a newly invented
    # role leaks nothing until it is deliberately trusted.
    classes = await _resolve([], "auditor", "anything")

    assert classes == _BOTH_CLASSES


async def test_builtin_default_is_empty_for_admin_when_no_rules() -> None:
    classes = await _resolve([], "admin", "anything")

    assert not classes


async def test_operator_is_trusted_and_unredacted_when_no_rules() -> None:
    classes = await _resolve([], "operator", "anything")

    assert not classes
