"""Unit tests for the server-side evidence redaction boundary (FAR-944, §2.2/§2.3).

Covers the two redaction classes -> JSON-key mapping, the
``apply_redaction`` semantics (sentinel replacement, no over-redaction, no
mutation, non-dict passthrough), and the rule-resolution precedence
(most-specific key_pattern -> ``*`` catch-all -> built-in viewer default).

``resolve_redaction_classes`` is exercised against a fake session that returns
the org's rules directly — the SQL role filter is the DB's job; these tests pin
the Python precedence logic on top of a role-homogeneous rule list.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

from modulo.core.evidence_redaction import (
    _DEFAULT_CLASSES_BY_ROLE,
    REDACTED_SENTINEL,
    REDACTION_CLASS_FIELDS,
    apply_redaction,
    resolve_redaction_classes,
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
    """Minimal session double returning a fixed, role-homogeneous rule list."""

    def __init__(self, rules: list[Any]) -> None:
        self._rules = rules

    async def execute(self, statement: Any) -> _FakeResult:
        return _FakeResult(self._rules)


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


def test_builtin_default_redacts_both_classes_for_viewer() -> None:
    assert _DEFAULT_CLASSES_BY_ROLE["viewer"] == _BOTH_CLASSES


# ---------------------------------------------------------------------------
# apply_redaction
# ---------------------------------------------------------------------------


def test_viewer_redacts_sensitive_output_but_keeps_permitted_fields() -> None:
    value = {"raw_output": "secret", "status": "complete", "pass": True, "run_id": "r1"}

    result = apply_redaction(value, "viewer", frozenset({"sensitive_output"}))

    assert result is not None
    assert result["raw_output"] == REDACTED_SENTINEL
    assert result["status"] == "complete"
    assert result["pass"] is True
    assert result["run_id"] == "r1"


def test_empty_class_set_leaves_value_unchanged_for_admin() -> None:
    value = {"raw_output": "secret", "config": {"threshold": 1}}

    result = apply_redaction(value, "admin", frozenset())

    assert result == value


def test_internal_metadata_class_redacts_its_fields_only() -> None:
    value = {"config": {"x": 1}, "node_id": "n1", "snapshot": {"id": "s"}, "status": "ok"}

    result = apply_redaction(value, "viewer", frozenset({"internal_metadata"}))

    assert result is not None
    assert result["config"] == REDACTED_SENTINEL
    assert result["node_id"] == REDACTED_SENTINEL
    assert result["snapshot"] == REDACTED_SENTINEL
    assert result["status"] == "ok"


def test_both_classes_redact_sensitive_and_internal_keys_together() -> None:
    value = {"raw_output": "x", "reasoning": "y", "config": {"a": 1}, "status": "ok", "run_id": "r"}

    result = apply_redaction(value, "viewer", _BOTH_CLASSES)

    assert result is not None
    assert result["raw_output"] == REDACTED_SENTINEL
    assert result["reasoning"] == REDACTED_SENTINEL
    assert result["config"] == REDACTED_SENTINEL
    assert result["status"] == "ok"
    assert result["run_id"] == "r"


def test_unknown_field_outside_classes_is_never_redacted() -> None:
    value = {"some_new_field": "visible", "raw_output": "hidden"}

    result = apply_redaction(value, "viewer", frozenset({"sensitive_output"}))

    assert result is not None
    assert result["some_new_field"] == "visible"
    assert result["raw_output"] == REDACTED_SENTINEL


def test_non_dict_value_is_returned_unchanged() -> None:
    assert apply_redaction(True, "viewer", _BOTH_CLASSES) is True
    assert apply_redaction(None, "viewer", _BOTH_CLASSES) is None


def test_apply_redaction_does_not_mutate_the_input_mapping() -> None:
    value = {"raw_output": "secret"}

    apply_redaction(value, "viewer", frozenset({"sensitive_output"}))

    assert value["raw_output"] == "secret"


# ---------------------------------------------------------------------------
# resolve_redaction_classes — precedence
# ---------------------------------------------------------------------------


async def test_most_specific_pattern_wins() -> None:
    rules = [
        _rule("*", "viewer", ["internal_metadata"]),
        _rule("llm_judge.*", "viewer", ["sensitive_output"]),
        _rule("llm_judge.grade", "viewer", ["sensitive_output", "internal_metadata"]),
    ]

    classes = await resolve_redaction_classes(_FakeSession(rules), _ORG_ID, "viewer", "llm_judge.grade")

    assert classes == _BOTH_CLASSES


async def test_longer_matching_pattern_wins_over_shorter() -> None:
    rules = [
        _rule("llm_judge.*", "viewer", ["sensitive_output"]),
        _rule("llm_judge.grade.*", "viewer", ["internal_metadata"]),
    ]

    classes = await resolve_redaction_classes(_FakeSession(rules), _ORG_ID, "viewer", "llm_judge.grade.score")

    assert classes == frozenset({"internal_metadata"})


async def test_catch_all_is_used_when_no_specific_pattern_matches() -> None:
    rules = [
        _rule("eval.*", "viewer", ["sensitive_output"]),
        _rule("*", "viewer", ["sensitive_output", "internal_metadata"]),
    ]

    classes = await resolve_redaction_classes(_FakeSession(rules), _ORG_ID, "viewer", "risk.api-call")

    assert classes == _BOTH_CLASSES


async def test_builtin_default_applies_when_rules_empty_for_viewer() -> None:
    classes = await resolve_redaction_classes(_FakeSession([]), _ORG_ID, "viewer", "anything")

    assert classes == _BOTH_CLASSES


async def test_builtin_default_is_empty_for_admin_when_no_rules() -> None:
    classes = await resolve_redaction_classes(_FakeSession([]), _ORG_ID, "admin", "anything")

    assert not classes


async def test_non_matching_pattern_without_catch_all_uses_builtin_default() -> None:
    rules = [_rule("eval.*", "viewer", ["sensitive_output"])]

    classes = await resolve_redaction_classes(_FakeSession(rules), _ORG_ID, "viewer", "other.key")

    assert classes == _BOTH_CLASSES
