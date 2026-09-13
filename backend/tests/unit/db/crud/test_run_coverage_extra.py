"""Unit tests for db/crud/run.py — pure functions and helpers.

Targets the uncovered branches identified by coverage: coalesce-key
extraction, idempotency helpers, reserved-key strip, ref canonicalisation,
pin/fingerprint helpers, payload-key injection, and status predicates.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modulo.db.crud.run import (
    COALESCE_KEY_FIELD,
    WorkItemRefsRequiredError,
    _canonicalise_ref_entries,
    _floor_work_item_id,
    _harvest_wire_refs,
    _has_guardrail_work,
    _has_pinned_guardrail_set,
    _inject_engine_payload_keys,
    _input_hash,
    _is_failure_bucket_status,
    _is_failure_reason_status,
    _is_terminal_status,
    _is_valid_int_limit_value,
    _is_valid_pin_entry,
    _merge_rank_guarded_refs,
    _pin_fingerprint_mismatch,
    _strip_reserved_keys,
    read_coalesce_key,
    run_idempotency_key,
    run_idempotency_ref,
)

# ---------------------------------------------------------------------------
# run_idempotency_ref / run_idempotency_key
# ---------------------------------------------------------------------------


def test_run_idempotency_ref_builds_stable_identity() -> None:
    pid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    ref = run_idempotency_ref(pid, 42)
    assert ref == "11111111-1111-1111-1111-111111111111:42"


def test_run_idempotency_key_returns_valid_ref() -> None:
    ref = f"{uuid.uuid4()}:7"
    assert run_idempotency_key(ref) == ref


def test_run_idempotency_key_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="run_ref must be"):
        run_idempotency_key(123)  # type: ignore[arg-type]


def test_run_idempotency_key_rejects_no_colon() -> None:
    with pytest.raises(ValueError, match="run_ref must be"):
        run_idempotency_key("no-colon-here")


def test_run_idempotency_key_rejects_negative_number() -> None:
    with pytest.raises(ValueError, match="run_ref must be"):
        run_idempotency_key(f"{uuid.uuid4()}:-1")


def test_run_idempotency_key_rejects_non_numeric_part() -> None:
    with pytest.raises(ValueError, match="run_ref must be"):
        run_idempotency_key(f"{uuid.uuid4()}:abc")


# ---------------------------------------------------------------------------
# read_coalesce_key
# ---------------------------------------------------------------------------


def test_read_coalesce_key_from_dict() -> None:
    key = "my-work-item-key"
    payload = {COALESCE_KEY_FIELD: key}
    assert read_coalesce_key(payload) == key


def test_read_coalesce_key_from_json_string() -> None:
    key = "string-key"
    payload = json.dumps({COALESCE_KEY_FIELD: key})
    assert read_coalesce_key(payload) == key


def test_read_coalesce_key_from_invalid_json_string() -> None:
    assert read_coalesce_key("{bad json}") is None


def test_read_coalesce_key_from_non_dict_non_string() -> None:
    assert read_coalesce_key(42) is None
    assert read_coalesce_key(None) is None
    assert read_coalesce_key([1, 2]) is None


def test_read_coalesce_key_absent_field() -> None:
    assert read_coalesce_key({"other_field": "value"}) is None


def test_read_coalesce_key_falsy_value() -> None:
    assert read_coalesce_key({COALESCE_KEY_FIELD: ""}) is None
    assert read_coalesce_key({COALESCE_KEY_FIELD: 0}) is None
    assert read_coalesce_key({COALESCE_KEY_FIELD: None}) is None


# ---------------------------------------------------------------------------
# _input_hash
# ---------------------------------------------------------------------------


def test_input_hash_stable_for_same_payload() -> None:
    payload = {"a": 1, "b": [2, 3]}
    h1 = _input_hash(payload)
    h2 = _input_hash(payload)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_input_hash_differs_for_different_payloads() -> None:
    h1 = _input_hash({"a": 1})
    h2 = _input_hash({"a": 2})
    assert h1 != h2


def test_input_hash_handles_non_stringable_values() -> None:
    # default=str serialisation of non-JSON types
    h = _input_hash({"uuid": uuid.UUID("00000000-0000-0000-0000-000000000001")})
    assert isinstance(h, str)


# ---------------------------------------------------------------------------
# _strip_reserved_keys
# ---------------------------------------------------------------------------


def test_strip_reserved_keys_removes_system_keys() -> None:
    from modulo.db.lifecycle_refs import WORK_ITEM_REFS_KEY

    payload = {
        "user_key": "keep",
        "_work_item_id": "remove",
        "_modulo.work_item": "remove",
        "_feedback_correction": "remove",
        "_coalesce_key": "remove",
        WORK_ITEM_REFS_KEY: [{"kind": "issue", "ref": "123"}],
    }
    stripped = _strip_reserved_keys(payload)
    assert stripped["user_key"] == "keep"
    assert WORK_ITEM_REFS_KEY in stripped  # exempted
    for key in ("_work_item_id", "_modulo.work_item", "_feedback_correction", "_coalesce_key"):
        assert key not in stripped


def test_strip_reserved_keys_preserves_work_item_refs() -> None:
    from modulo.db.lifecycle_refs import WORK_ITEM_REFS_KEY

    refs = [{"kind": "issue", "ref": "ABC"}]
    payload = {WORK_ITEM_REFS_KEY: refs, "_coalesce_key": "gone"}
    stripped = _strip_reserved_keys(payload)
    assert stripped[WORK_ITEM_REFS_KEY] == refs


# ---------------------------------------------------------------------------
# _canonicalise_ref_entries
# ---------------------------------------------------------------------------


def test_canonicalise_ref_entries_empty() -> None:
    assert _canonicalise_ref_entries(None) is None
    assert _canonicalise_ref_entries([]) is None


def test_canonicalise_ref_entries_valid() -> None:
    entries = [{"kind": "issue", "ref": "FAR-100", "source": "caller"}]
    result = _canonicalise_ref_entries(entries)
    assert result is not None
    assert len(result) == 1
    assert result[0]["kind"] == "issue"


def test_canonicalise_ref_entries_with_force_source() -> None:
    entries = [{"kind": "issue", "ref": "FAR-100", "source": "wrong"}]
    result = _canonicalise_ref_entries(entries, force_source="derived")
    assert result is not None
    # Source is overwritten to the forced source
    assert result[0]["source"] == "derived"


def test_canonicalise_ref_entries_drops_malformed() -> None:
    entries = [42, "not-a-dict", {"kind": "issue", "ref": "FAR-100"}]
    result = _canonicalise_ref_entries(entries)
    # Malformed entries are dropped; the valid one survives
    assert result is not None
    assert len(result) == 1


def test_canonicalise_ref_entries_drops_malformed_with_force_source() -> None:
    entries = [42, {"kind": "issue", "ref": "FAR-100", "source": "caller"}]
    result = _canonicalise_ref_entries(entries, force_source="derived")
    assert result is not None
    assert result[0]["source"] == "derived"


# ---------------------------------------------------------------------------
# _harvest_wire_refs
# ---------------------------------------------------------------------------


def test_harvest_wire_refs_prefers_system_carrier() -> None:
    from modulo.db.lifecycle_refs import WIRE_REFS_ALIAS_KEY, WORK_ITEM_REFS_KEY

    refs = [{"kind": "issue", "ref": "1"}]
    payload = {
        WORK_ITEM_REFS_KEY: refs,
        WIRE_REFS_ALIAS_KEY: [{"kind": "issue", "ref": "2"}],
    }
    assert _harvest_wire_refs(payload) is refs


def test_harvest_wire_refs_falls_back_to_wire_alias() -> None:
    from modulo.db.lifecycle_refs import WIRE_REFS_ALIAS_KEY

    refs = [{"kind": "issue", "ref": "3"}]
    payload = {WIRE_REFS_ALIAS_KEY: refs}
    assert _harvest_wire_refs(payload) is refs


def test_harvest_wire_refs_returns_none_when_absent() -> None:
    assert _harvest_wire_refs({"unrelated": True}) is None


def test_harvest_wire_refs_ignores_non_list() -> None:
    from modulo.db.lifecycle_refs import WORK_ITEM_REFS_KEY

    assert _harvest_wire_refs({WORK_ITEM_REFS_KEY: "not-a-list"}) is None


# ---------------------------------------------------------------------------
# _merge_rank_guarded_refs
# ---------------------------------------------------------------------------


def test_merge_rank_guarded_refs_empty() -> None:
    assert _merge_rank_guarded_refs(None, None) is None


def test_merge_rank_guarded_refs_existing_only() -> None:
    refs = [{"kind": "issue", "ref": "FAR-100", "source": "caller"}]
    result = _merge_rank_guarded_refs(refs, None)
    assert result is not None
    assert len(result) == 1


def test_merge_rank_guarded_refs_incoming_only() -> None:
    refs = [{"kind": "issue", "ref": "FAR-100", "source": "derived"}]
    result = _merge_rank_guarded_refs(None, refs)
    assert result is not None
    assert len(result) == 1


def test_merge_rank_guarded_refs_higher_rank_wins() -> None:
    existing = [{"kind": "issue", "ref": "FAR-100", "source": "agent"}]
    incoming = [{"kind": "issue", "ref": "FAR-100", "source": "caller"}]
    result = _merge_rank_guarded_refs(existing, incoming)
    assert result is not None
    assert len(result) == 1
    assert result[0]["source"] == "caller"


def test_merge_rank_guarded_refs_tie_keeps_existing() -> None:
    existing = [{"kind": "issue", "ref": "FAR-100", "source": "derived"}]
    incoming = [{"kind": "issue", "ref": "FAR-100", "source": "derived"}]
    result = _merge_rank_guarded_refs(existing, incoming)
    assert result is not None
    assert len(result) == 1


def test_merge_rank_guarded_refs_ignores_malformed_entries() -> None:
    refs = [{"kind": "issue", "ref": "FAR-100", "source": "caller"}, "garbage", 42]
    result = _merge_rank_guarded_refs(refs, None)
    assert result is not None
    assert len(result) == 1


def test_merge_rank_guarded_refs_deduplicates_different_refs() -> None:
    existing = [{"kind": "issue", "ref": "FAR-100", "source": "caller"}]
    incoming = [{"kind": "pr", "ref": "42", "source": "agent"}]
    result = _merge_rank_guarded_refs(existing, incoming)
    assert result is not None
    assert len(result) == 2


# ---------------------------------------------------------------------------
# _floor_work_item_id
# ---------------------------------------------------------------------------


def test_floor_work_item_id_deterministic() -> None:
    org = uuid.uuid4()
    run = uuid.uuid4()
    id1 = _floor_work_item_id(org, run)
    id2 = _floor_work_item_id(org, run)
    assert id1 == id2


def test_floor_work_item_id_differs_for_different_inputs() -> None:
    org = uuid.uuid4()
    run1 = uuid.uuid4()
    run2 = uuid.uuid4()
    assert _floor_work_item_id(org, run1) != _floor_work_item_id(org, run2)


# ---------------------------------------------------------------------------
# Status predicates
# ---------------------------------------------------------------------------


def test_is_terminal_status() -> None:
    assert _is_terminal_status("complete")
    assert _is_terminal_status("failed")
    assert _is_terminal_status("cancelled")
    assert not _is_terminal_status("running")
    assert not _is_terminal_status("pending")


def test_is_failure_bucket_status() -> None:
    assert _is_failure_bucket_status("failed")
    assert _is_failure_bucket_status("cancelled")
    assert _is_failure_bucket_status("eval_failed")
    assert _is_failure_bucket_status("expired")
    assert not _is_failure_bucket_status("complete")


def test_is_failure_reason_status() -> None:
    assert _is_failure_reason_status("failed")
    assert _is_failure_reason_status("stalled")
    assert _is_failure_reason_status("eval_failed")
    assert not _is_failure_reason_status("cancelled")


# ---------------------------------------------------------------------------
# _is_valid_int_limit_value
# ---------------------------------------------------------------------------


def test_is_valid_int_limit_value_true() -> None:
    assert _is_valid_int_limit_value(10)
    assert _is_valid_int_limit_value(0)
    assert _is_valid_int_limit_value(-1)


def test_is_valid_int_limit_value_false() -> None:
    assert not _is_valid_int_limit_value(True)
    assert not _is_valid_int_limit_value(False)
    assert not _is_valid_int_limit_value("10")
    assert not _is_valid_int_limit_value(3.14)
    assert not _is_valid_int_limit_value(None)


# ---------------------------------------------------------------------------
# _has_guardrail_work
# ---------------------------------------------------------------------------


def test_has_guardrail_work_false_when_all_empty() -> None:
    assert not _has_guardrail_work([], [], [], False)


def test_has_guardrail_work_true_for_guardrail_rows() -> None:
    assert _has_guardrail_work([MagicMock()], [], [], False)


def test_has_guardrail_work_true_for_pinned_defs() -> None:
    assert _has_guardrail_work([], [MagicMock()], [], False)


def test_has_guardrail_work_true_for_skipped() -> None:
    assert _has_guardrail_work([], [], [MagicMock()], False)


def test_has_guardrail_work_true_for_blocked() -> None:
    assert _has_guardrail_work([], [], [], True)


# ---------------------------------------------------------------------------
# _has_pinned_guardrail_set / _pin_fingerprint_mismatch
# ---------------------------------------------------------------------------


def test_has_pinned_guardrail_set_with_pins() -> None:
    assert _has_pinned_guardrail_set([{"name": "x"}], None)


def test_has_pinned_guardrail_set_with_fingerprint() -> None:
    assert _has_pinned_guardrail_set(None, "abc123")


def test_has_pinned_guardrail_set_empty() -> None:
    assert not _has_pinned_guardrail_set(None, None)
    assert not _has_pinned_guardrail_set([], None)


def test_pin_fingerprint_mismatch_true() -> None:
    assert _pin_fingerprint_mismatch("old", "new")


def test_pin_fingerprint_mismatch_false() -> None:
    assert not _pin_fingerprint_mismatch("same", "same")
    assert not _pin_fingerprint_mismatch(None, "same")
    assert not _pin_fingerprint_mismatch(None, None)
    # saved_fingerprint is set but recomputed is None → mismatch (saved≠recomputed)
    assert _pin_fingerprint_mismatch("same", None)


# ---------------------------------------------------------------------------
# _is_valid_pin_entry
# ---------------------------------------------------------------------------


def test_is_valid_pin_entry_valid() -> None:
    assert _is_valid_pin_entry({"name": "my-guardrail"})


def test_is_valid_pin_entry_no_name() -> None:
    assert not _is_valid_pin_entry({"name": ""})
    assert not _is_valid_pin_entry({"name": None})
    assert not _is_valid_pin_entry({})
    assert not _is_valid_pin_entry("not-a-dict")


# ---------------------------------------------------------------------------
# _inject_engine_payload_keys
# ---------------------------------------------------------------------------


def test_inject_engine_payload_keys_both_present() -> None:
    payload: dict[str, Any] = {}
    fb = {"correction": "yes"}
    ck = "coalesce-123"
    result = _inject_engine_payload_keys(payload, fb, ck)
    assert result["_feedback_correction"] == fb
    assert result[COALESCE_KEY_FIELD] == ck


def test_inject_engine_payload_keys_neither_present() -> None:
    payload: dict[str, Any] = {"existing": True}
    result = _inject_engine_payload_keys(payload, None, None)
    assert "existing" in result
    assert "_feedback_correction" not in result
    assert COALESCE_KEY_FIELD not in result


def test_inject_engine_payload_keys_only_correction() -> None:
    payload: dict[str, Any] = {}
    fb = {"x": 1}
    result = _inject_engine_payload_keys(payload, fb, None)
    assert result["_feedback_correction"] == fb
    assert COALESCE_KEY_FIELD not in result


def test_inject_engine_payload_keys_only_coalesce() -> None:
    payload: dict[str, Any] = {}
    result = _inject_engine_payload_keys(payload, None, "ck-1")
    assert "_feedback_correction" not in result
    assert result[COALESCE_KEY_FIELD] == "ck-1"


# ---------------------------------------------------------------------------
# WorkItemRefsRequiredError
# ---------------------------------------------------------------------------


def test_work_item_refs_required_error_message() -> None:
    pid = uuid.uuid4()
    exc = WorkItemRefsRequiredError(pid)
    assert str(pid) in str(exc)
    assert exc.pipeline_id == pid


# ---------------------------------------------------------------------------
# _select_guardrail_definitions (no pinned set → live rows)
# ---------------------------------------------------------------------------


def test_select_guardrail_definitions_no_pins_uses_live_rows() -> None:
    from modulo.db.crud.run import _select_guardrail_definitions

    mock_row = MagicMock()
    with patch("modulo.core.guardrails.to_engine_definition", return_value="defn") as mock_ted:
        result = _select_guardrail_definitions([mock_row], [], None, None)
    mock_ted.assert_called_once_with(mock_row)
    assert result == ["defn"]


def test_select_guardrail_definitions_with_pins_returns_pinned() -> None:
    from modulo.db.crud.run import _select_guardrail_definitions

    result = _select_guardrail_definitions([], ["pinned-def"], [{"name": "x"}], None)
    assert result == ["pinned-def"]
