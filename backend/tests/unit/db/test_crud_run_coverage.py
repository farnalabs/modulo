"""Targeted coverage tests for db.crud.run pure + mockable functions.

Focus: functions with high uncovered line counts that can be tested without
a real database — pure logic, dataclass helpers, and async functions with
mockable session reads.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.run import (
    _SANDBOX_CONCURRENCY_DEFAULT,
    _SANDBOX_CONCURRENCY_KEY,
    _SANDBOX_CONCURRENCY_MAX,
    CAPACITY_MARKERS,
    COALESCE_KEY_FIELD,
    RUN_STATUS_WHITELIST,
    SandboxConcurrencyLimit,
    WorkItemRefsRequiredError,
    _active_run_statuses,
    _canonicalise_ref_entries,
    _completed_durations_ms,
    _duration_by_day,
    _empty_run_stats,
    _failure_reason_counts,
    _floor_work_item_id,
    _graph_contains_sandbox_agent,
    _harvest_wire_refs,
    _has_guardrail_work,
    _has_pinned_guardrail_set,
    _inject_engine_payload_keys,
    _is_failure_bucket_status,
    _is_failure_reason_status,
    _is_terminal_status,
    _is_valid_int_limit_value,
    _is_valid_pin_entry,
    _json_bind,
    _merge_rank_guarded_refs,
    _percentile,
    _pin_fingerprint_mismatch,
    _resolve_sandbox_concurrency_limit,
    _runs_by_day,
    _stamp_guardrail_blocked_run,
    _strip_reserved_keys,
    read_coalesce_key,
    run_idempotency_key,
    run_idempotency_ref,
)
from modulo.db.models.run import (
    TERMINAL_STATUSES,
)

# ---------------------------------------------------------------------------
# _strip_reserved_keys
# ---------------------------------------------------------------------------


class TestStripReservedKeys:
    def test_strips_work_item_id(self):
        payload = {"key": "value", "_work_item_id": "abc"}
        result = _strip_reserved_keys(payload)
        assert "_work_item_id" not in result
        assert result["key"] == "value"

    def test_strips_feedback_correction(self):
        payload = {"key": "value", "_feedback_correction": {"foo": 1}}
        result = _strip_reserved_keys(payload)
        assert "_feedback_correction" not in result

    def test_strips_coalesce_key(self):
        payload = {"key": "value", "_coalesce_key": "ck-123"}
        result = _strip_reserved_keys(payload)
        assert "_coalesce_key" not in result

    def test_preserves_work_item_refs(self):
        """_work_item_refs is reserved but EXEMPT from the strip."""
        refs = [{"kind": "linear", "ref": "FAR-1"}]
        payload = {"key": "value", "_work_item_refs": refs}
        result = _strip_reserved_keys(payload)
        assert result["_work_item_refs"] == refs

    def test_empty_payload(self):
        assert not _strip_reserved_keys({})

    def test_non_reserved_keys_pass_through(self):
        payload = {"user_data": [1, 2, 3], "name": "test"}
        result = _strip_reserved_keys(payload)
        assert result == payload


# ---------------------------------------------------------------------------
# read_coalesce_key
# ---------------------------------------------------------------------------


class TestReadCoalesceKey:
    def test_read_from_dict(self):
        payload = {COALESCE_KEY_FIELD: "ck-42"}
        assert read_coalesce_key(payload) == "ck-42"

    def test_read_from_json_string(self):
        payload = json.dumps({COALESCE_KEY_FIELD: "ck-99"})
        assert read_coalesce_key(payload) == "ck-99"

    def test_none_payload(self):
        assert read_coalesce_key(None) is None

    def test_non_dict_payload(self):
        assert read_coalesce_key([1, 2, 3]) is None

    def test_missing_key(self):
        assert read_coalesce_key({"other": "value"}) is None

    def test_empty_string_value_returns_none(self):
        """Falsy string value returns None (not empty string)."""
        assert read_coalesce_key({COALESCE_KEY_FIELD: ""}) is None

    def test_zero_value_returns_none(self):
        """Falsy zero value returns None."""
        assert read_coalesce_key({COALESCE_KEY_FIELD: 0}) is None

    def test_non_parseable_json_string(self):
        assert read_coalesce_key("not json at all") is None

    def test_numeric_value_coerced_to_string(self):
        assert read_coalesce_key({COALESCE_KEY_FIELD: 123}) == "123"


# ---------------------------------------------------------------------------
# _harvest_wire_refs
# ---------------------------------------------------------------------------


class TestHarvestWireRefs:
    def test_prefers_work_item_refs_key(self):
        payload = {"_work_item_refs": [{"kind": "a"}], "work_item_refs": [{"kind": "b"}]}
        assert _harvest_wire_refs(payload) == [{"kind": "a"}]

    def test_falls_back_to_wire_alias(self):
        payload = {"work_item_refs": [{"kind": "b"}]}
        assert _harvest_wire_refs(payload) == [{"kind": "b"}]

    def test_returns_none_when_absent(self):
        assert _harvest_wire_refs({"other": "value"}) is None

    def test_returns_none_when_not_list(self):
        assert _harvest_wire_refs({"_work_item_refs": "not-a-list"}) is None

    def test_returns_none_for_empty_payload(self):
        assert _harvest_wire_refs({}) is None


# ---------------------------------------------------------------------------
# _merge_rank_guarded_refs
# ---------------------------------------------------------------------------


class TestMergeRankGuardedRefs:
    def test_both_none(self):
        assert _merge_rank_guarded_refs(None, None) is None

    def test_existing_only(self):
        existing = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        assert _merge_rank_guarded_refs(existing, None) is not None

    def test_incoming_only(self):
        incoming = [{"kind": "issue", "ref": "FAR-2", "source": "derived"}]
        assert _merge_rank_guarded_refs(None, incoming) is not None

    def test_higher_rank_wins(self):
        existing = [{"kind": "issue", "ref": "FAR-1", "source": "agent"}]
        incoming = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        result = _merge_rank_guarded_refs(existing, incoming)
        assert result is not None
        assert any(e["source"] == "caller" for e in result)

    def test_same_rank_keeps_existing(self):
        existing = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        incoming = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        result = _merge_rank_guarded_refs(existing, incoming)
        assert result is not None

    def test_different_refs_union(self):
        existing = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        incoming = [{"kind": "issue", "ref": "FAR-2", "source": "derived"}]
        result = _merge_rank_guarded_refs(existing, incoming)
        assert result is not None
        refs = {e["ref"] for e in result}
        assert "FAR-1" in refs
        assert "FAR-2" in refs

    def test_invalid_entry_ignored(self):
        existing = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        incoming = [{"not_kind": "x"}]
        result = _merge_rank_guarded_refs(existing, incoming)
        assert result is not None
        assert len(result) == 1

    def test_empty_dicts_ignored(self):
        assert _merge_rank_guarded_refs([{}], [{}]) is None


# ---------------------------------------------------------------------------
# _canonicalise_ref_entries
# ---------------------------------------------------------------------------


class TestCanonicaliseRefEntries:
    def test_none_input(self):
        assert _canonicalise_ref_entries(None) is None

    def test_empty_list(self):
        assert _canonicalise_ref_entries([]) is None

    def test_valid_entries(self):
        entries = [{"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        result = _canonicalise_ref_entries(entries)
        assert result is not None
        assert len(result) == 1

    def test_malformed_entry_dropped(self):
        entries = [{"bad": "entry"}, {"kind": "issue", "ref": "FAR-1", "source": "caller"}]
        result = _canonicalise_ref_entries(entries)
        assert result is not None
        assert len(result) == 1

    def test_force_source_stamps_entries(self):
        entries = [{"kind": "issue", "ref": "FAR-1", "source": "wrong"}]
        result = _canonicalise_ref_entries(entries, force_source="derived")
        assert result is not None
        assert result[0]["source"] == "derived"

    def test_force_source_counts_unknown(self):
        entries = [{"kind": "issue", "ref": "FAR-1", "source": "wrong"}]
        with patch("modulo.db.crud.run.notify_refs_event") as mock_notify:
            _canonicalise_ref_entries(entries, force_source="derived")
            # Should have called notify for unknown_source
            assert mock_notify.call_count >= 1


# ---------------------------------------------------------------------------
# _has_guardrail_work / _has_pinned_guardrail_set / _pin_fingerprint_mismatch / _is_valid_pin_entry
# ---------------------------------------------------------------------------


class TestGuardrailHelpers:
    def test_has_guardrail_work_with_rows(self):
        assert _has_guardrail_work(["row"], [], [], False) is True

    def test_has_guardrail_work_with_pinned_defs(self):
        assert _has_guardrail_work([], ["def"], [], False) is True

    def test_has_guardrail_work_with_skipped(self):
        assert _has_guardrail_work([], [], ["skip"], False) is True

    def test_has_guardrail_work_with_blocked(self):
        assert _has_guardrail_work([], [], [], True) is True

    def test_has_guardrail_work_empty(self):
        assert _has_guardrail_work([], [], [], False) is False

    def test_has_pinned_guardrail_set_with_pins(self):
        assert _has_pinned_guardrail_set([{"name": "x"}], None) is True

    def test_has_pinned_guardrail_set_with_fingerprint(self):
        assert _has_pinned_guardrail_set(None, "abc123") is True

    def test_has_pinned_guardrail_set_neither(self):
        assert _has_pinned_guardrail_set(None, None) is False

    def test_pin_fingerprint_mismatch_when_match(self):
        assert _pin_fingerprint_mismatch("abc", "abc") is False

    def test_pin_fingerprint_mismatch_when_mismatch(self):
        assert _pin_fingerprint_mismatch("abc", "def") is True

    def test_pin_fingerprint_mismatch_saved_none(self):
        assert _pin_fingerprint_mismatch(None, "anything") is False

    def test_is_valid_pin_entry_dict_with_name(self):
        assert _is_valid_pin_entry({"name": "guardrail-1"}) is True

    def test_is_valid_pin_entry_dict_without_name(self):
        assert _is_valid_pin_entry({"other": "value"}) is False

    def test_is_valid_pin_entry_non_dict(self):
        assert _is_valid_pin_entry("not-a-dict") is False

    def test_is_valid_pin_entry_empty_name(self):
        assert _is_valid_pin_entry({"name": ""}) is False


# ---------------------------------------------------------------------------
# _active_run_statuses / _is_terminal_status / _is_failure_* / _is_valid_int_limit_value
# ---------------------------------------------------------------------------


class TestStatusHelpers:
    def test_active_run_statuses_includes_pending(self):
        assert "pending" in _active_run_statuses(True)

    def test_active_run_statuses_excludes_pending(self):
        assert "pending" not in _active_run_statuses(False)

    def test_active_run_statuses_returns_set(self):
        assert isinstance(_active_run_statuses(True), set)

    def test_is_terminal_status_complete(self):
        assert _is_terminal_status("complete") is True

    def test_is_terminal_status_failed(self):
        assert _is_terminal_status("failed") is True

    def test_is_terminal_status_running(self):
        assert _is_terminal_status("running") is False

    def test_is_failure_bucket_status_failed(self):
        assert _is_failure_bucket_status("failed") is True

    def test_is_failure_bucket_status_complete(self):
        assert _is_failure_bucket_status("complete") is False

    def test_is_failure_reason_status_eval_failed(self):
        assert _is_failure_reason_status("eval_failed") is True

    def test_is_failure_reason_status_complete(self):
        assert _is_failure_reason_status("complete") is False

    def test_is_valid_int_limit_value_int(self):
        assert _is_valid_int_limit_value(42) is True

    def test_is_valid_int_limit_value_bool(self):
        assert _is_valid_int_limit_value(True) is False
        assert _is_valid_int_limit_value(False) is False

    def test_is_valid_int_limit_value_string(self):
        assert _is_valid_int_limit_value("42") is False

    def test_is_valid_int_limit_value_float(self):
        assert _is_valid_int_limit_value(3.14) is False

    def test_is_valid_int_limit_value_none(self):
        assert _is_valid_int_limit_value(None) is False


# ---------------------------------------------------------------------------
# _graph_contains_sandbox_agent
# ---------------------------------------------------------------------------


class TestGraphContainsSandboxAgent:
    def test_none_input(self):
        assert _graph_contains_sandbox_agent(None) is False

    def test_non_dict_input(self):
        assert _graph_contains_sandbox_agent("not a dict") is False

    def test_no_nodes_key(self):
        assert _graph_contains_sandbox_agent({"edges": []}) is False

    def test_nodes_not_list(self):
        assert _graph_contains_sandbox_agent({"nodes": "not-a-list"}) is False

    def test_sandbox_agent_found(self):
        graph = {"nodes": [{"node_type": "sandbox_agent"}]}
        assert _graph_contains_sandbox_agent(graph) is True

    def test_no_sandbox_agent(self):
        graph = {"nodes": [{"node_type": "agent"}]}
        assert _graph_contains_sandbox_agent(graph) is False

    def test_empty_nodes(self):
        assert _graph_contains_sandbox_agent({"nodes": []}) is False

    def test_non_dict_node(self):
        assert _graph_contains_sandbox_agent({"nodes": ["not-a-dict"]}) is False


# ---------------------------------------------------------------------------
# _percentile / _empty_run_stats / _floor_work_item_id
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_list(self):
        assert _percentile([], 50) == 0.0

    def test_single_element(self):
        assert _percentile([10.0], 50) == 10.0

    def test_p50_of_even_list(self):
        result = _percentile([1.0, 2.0, 3.0, 4.0], 50)
        assert result == pytest.approx(2.5)

    def test_p95(self):
        data = [float(i) for i in range(100)]
        result = _percentile(data, 95)
        assert 94.0 <= result <= 96.0

    def test_p0(self):
        assert _percentile([10.0, 20.0, 30.0], 0) == 10.0

    def test_p100(self):
        assert _percentile([10.0, 20.0, 30.0], 100) == 30.0


class TestEmptyRunStats:
    def test_shape(self):
        stats = _empty_run_stats()
        assert stats["total_runs"] == 0
        assert stats["success_rate"] == 0.0
        assert not stats["runs_by_day"]
        assert not stats["failure_by_reason"]
        assert not stats["avg_duration_by_day"]


class TestFloorWorkItemId:
    def test_deterministic(self):
        org = uuid.uuid4()
        run = uuid.uuid4()
        assert _floor_work_item_id(org, run) == _floor_work_item_id(org, run)

    def test_different_org_different_id(self):
        run = uuid.uuid4()
        id1 = _floor_work_item_id(uuid.uuid4(), run)
        id2 = _floor_work_item_id(uuid.uuid4(), run)
        assert id1 != id2

    def test_different_run_different_id(self):
        org = uuid.uuid4()
        id1 = _floor_work_item_id(org, uuid.uuid4())
        id2 = _floor_work_item_id(org, uuid.uuid4())
        assert id1 != id2

    def test_uses_correct_namespace(self):
        """Verify the floor ID is a uuid5 in the correct namespace."""
        org = uuid.uuid4()
        run = uuid.uuid4()
        result = _floor_work_item_id(org, run)
        assert result.version == 5


# ---------------------------------------------------------------------------
# _completed_durations_ms / _runs_by_day / _failure_reason_counts / _duration_by_day
# ---------------------------------------------------------------------------


class TestCompletedDurationsMs:
    def test_filters_none_timestamps(self):
        now = datetime.now(UTC)
        runs = [
            SimpleNamespace(started_at=now, completed_at=now + timedelta(seconds=1)),
            SimpleNamespace(started_at=None, completed_at=now),
            SimpleNamespace(started_at=now, completed_at=None),
        ]
        result = _completed_durations_ms(runs)
        assert len(result) == 1
        assert result[0] == 1000

    def test_sorted_ascending(self):
        now = datetime.now(UTC)
        runs = [
            SimpleNamespace(started_at=now, completed_at=now + timedelta(seconds=3)),
            SimpleNamespace(started_at=now, completed_at=now + timedelta(seconds=1)),
        ]
        result = _completed_durations_ms(runs)
        assert result == [1000, 3000]

    def test_empty_list(self):
        assert not _completed_durations_ms([])


class TestRunsByDay:
    def test_groups_by_day(self):
        day1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        day2 = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
        runs = [
            SimpleNamespace(status="complete", created_at=day1),
            SimpleNamespace(status="complete", created_at=day1),
            SimpleNamespace(status="failed", created_at=day2),
        ]
        result = _runs_by_day(runs)
        assert result["2024-01-01"]["count"] == 2
        assert result["2024-01-01"]["success"] == 2
        assert result["2024-01-02"]["failed"] == 1

    def test_empty_list(self):
        assert not _runs_by_day([])


class TestFailureReasonCounts:
    def test_counts_failure_codes(self):
        runs = [
            SimpleNamespace(status="failed", error_code="rate_limit"),
            SimpleNamespace(status="failed", error_code="rate_limit"),
            SimpleNamespace(status="failed", error_code="timeout"),
            SimpleNamespace(status="complete", error_code="rate_limit"),
        ]
        result = _failure_reason_counts(runs)
        assert result["rate_limit"] == 2  # 2 failed with rate_limit (complete doesn't count)
        assert result["timeout"] == 1

    def test_ignores_non_failure_statuses(self):
        runs = [SimpleNamespace(status="complete", error_code="rate_limit")]
        result = _failure_reason_counts(runs)
        assert len(result) == 0

    def test_empty_list(self):
        assert not _failure_reason_counts([])


class TestDurationByDay:
    def test_groups_by_day(self):
        day1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        runs = [
            SimpleNamespace(
                created_at=day1,
                started_at=day1,
                completed_at=day1 + timedelta(seconds=10),
            ),
            SimpleNamespace(
                created_at=day1,
                started_at=day1,
                completed_at=day1 + timedelta(seconds=20),
            ),
        ]
        result = _duration_by_day(runs)
        assert len(result["2024-01-01"]) == 2

    def test_filters_none_timestamps(self):
        day1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        runs = [
            SimpleNamespace(created_at=day1, started_at=None, completed_at=day1),
            SimpleNamespace(created_at=day1, started_at=day1, completed_at=None),
        ]
        result = _duration_by_day(runs)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# _json_bind
# ---------------------------------------------------------------------------


class TestJsonBind:
    def test_none(self):
        assert _json_bind(None) is None

    def test_string_passthrough(self):
        assert _json_bind('{"key": "value"}') == '{"key": "value"}'

    def test_dict_serializes(self):
        result = _json_bind({"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_list_serializes(self):
        result = _json_bind([1, 2, 3])
        assert json.loads(result) == [1, 2, 3]

    def test_bytes_decoded(self):
        assert _json_bind(b'{"key": "value"}') == '{"key": "value"}'


# ---------------------------------------------------------------------------
# _resolve_sandbox_concurrency_limit / SandboxConcurrencyLimit
# ---------------------------------------------------------------------------


class TestSandboxConcurrencyLimit:
    def test_enforced_cap_with_explicit_value(self):
        limit = SandboxConcurrencyLimit(cap=5)
        assert limit.enforced_cap == 5

    def test_enforced_cap_with_none(self):
        limit = SandboxConcurrencyLimit(cap=None)
        assert limit.enforced_cap is None

    def test_enforced_cap_default_flag_returns_none(self):
        limit = SandboxConcurrencyLimit(cap=4, is_default=True)
        assert limit.enforced_cap is None

    def test_enforced_cap_zero_deny_all(self):
        limit = SandboxConcurrencyLimit(cap=0)
        assert limit.enforced_cap == 0


class TestResolveSandboxConcurrencyLimit:
    def test_non_dict_settings(self):
        result = _resolve_sandbox_concurrency_limit("bad", uuid.uuid4())
        assert result.cap is None

    def test_missing_key_returns_default(self):
        result = _resolve_sandbox_concurrency_limit({}, uuid.uuid4())
        assert result.cap == _SANDBOX_CONCURRENCY_DEFAULT
        assert result.is_default is True

    def test_explicit_none_returns_no_cap(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: None}, uuid.uuid4())
        assert result.cap is None
        assert result.is_default is False

    def test_explicit_int(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: 8}, uuid.uuid4())
        assert result.cap == 8

    def test_bool_rejected(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: True}, uuid.uuid4())
        assert result.cap is None

    def test_string_rejected(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: "10"}, uuid.uuid4())
        assert result.cap is None

    def test_clamps_above_max(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: 200}, uuid.uuid4())
        assert result.cap == _SANDBOX_CONCURRENCY_MAX

    def test_clamps_below_zero(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: -5}, uuid.uuid4())
        assert result.cap == 0

    def test_boundary_zero(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: 0}, uuid.uuid4())
        assert result.cap == 0

    def test_boundary_max(self):
        result = _resolve_sandbox_concurrency_limit({_SANDBOX_CONCURRENCY_KEY: _SANDBOX_CONCURRENCY_MAX}, uuid.uuid4())
        assert result.cap == _SANDBOX_CONCURRENCY_MAX


# ---------------------------------------------------------------------------
# _inject_engine_payload_keys
# ---------------------------------------------------------------------------


class TestInjectEnginePayloadKeys:
    def test_injects_feedback_correction(self):
        payload = {"key": "value"}
        result = _inject_engine_payload_keys(payload, {"correction": True}, None)
        assert result["_feedback_correction"] == {"correction": True}

    def test_injects_coalesce_key(self):
        payload = {"key": "value"}
        result = _inject_engine_payload_keys(payload, None, "ck-123")
        assert result[COALESCE_KEY_FIELD] == "ck-123"

    def test_injects_both(self):
        payload = {"key": "value"}
        result = _inject_engine_payload_keys(payload, {"a": 1}, "ck-42")
        assert result["_feedback_correction"] == {"a": 1}
        assert result[COALESCE_KEY_FIELD] == "ck-42"

    def test_neither_none(self):
        payload = {"key": "value"}
        result = _inject_engine_payload_keys(payload, None, None)
        assert result == {"key": "value"}

    def test_empty_payload(self):
        result = _inject_engine_payload_keys({}, {"x": 1}, "ck")
        assert "_feedback_correction" in result
        assert COALESCE_KEY_FIELD in result


# ---------------------------------------------------------------------------
# _stamp_guardrail_blocked_run
# ---------------------------------------------------------------------------


class TestStampGuardrailBlockedRun:
    def test_sets_eval_failed_status(self):
        run = SimpleNamespace(
            status="pending",
            error_code=None,
            error_detail=None,
            completed_at=None,
        )
        _stamp_guardrail_blocked_run(run, "blocked by guardrail X")
        assert run.status == "eval_failed"
        assert run.error_code == "eval_blocked"
        assert run.error_detail == "blocked by guardrail X"
        assert run.completed_at is not None


# ---------------------------------------------------------------------------
# run_idempotency_ref / run_idempotency_key
# ---------------------------------------------------------------------------


class TestRunIdempotencyRef:
    def test_format(self):
        pid = uuid.uuid4()
        ref = run_idempotency_ref(pid, 42)
        assert ref == f"{pid}:42"

    def test_key_validates_correct_format(self):
        pid = uuid.uuid4()
        ref = run_idempotency_ref(pid, 1)
        assert run_idempotency_key(ref) == ref

    def test_key_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="run_ref must be"):
            run_idempotency_key("not-a-valid-ref")

    def test_key_rejects_non_string(self):
        with pytest.raises(ValueError, match="run_ref must be"):
            run_idempotency_key(123)

    def test_key_rejects_per_replay_id(self):
        """A UUID (per-replay run_id) must be rejected — it mints fresh keys."""
        with pytest.raises(ValueError, match="run_ref must be"):
            run_idempotency_key(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# WorkItemRefsRequiredError
# ---------------------------------------------------------------------------


class TestWorkItemRefsRequiredError:
    def test_message(self):
        pid = uuid.uuid4()
        err = WorkItemRefsRequiredError(pid)
        assert str(pid) in str(err)
        assert err.pipeline_id == pid


# ---------------------------------------------------------------------------
# _RUN_STATUS_WHITELIST / CAPACITY_MARKERS
# ---------------------------------------------------------------------------


class TestRunStatusWhitelist:
    def test_contains_expected_statuses(self):
        expected = {
            "pending",
            "running",
            "awaiting_human",
            "claimed",
            "unknown",
            "hitl_parked",
            "complete",
            "failed",
            "cancelled",
            "eval_failed",
            "stalled",
            "budget_exceeded",
            "router_no_match",
            "cost_ceiling_exceeded",
            "compensation_failed",
        }
        assert expected == RUN_STATUS_WHITELIST

    def test_capacity_markers_distinct_from_terminal(self):
        assert not CAPACITY_MARKERS.intersection(TERMINAL_STATUSES)


# ---------------------------------------------------------------------------
# _allocate_run_number (async, mocked)
# ---------------------------------------------------------------------------


class TestAllocateRunNumber:
    @pytest.mark.asyncio
    async def test_postgres_path(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 5
        session.execute = AsyncMock(return_value=mock_result)

        with patch("modulo.db.crud.run._get_dialect_name", new_callable=AsyncMock, return_value="postgresql"):
            from modulo.db.crud.run import _allocate_run_number

            result = await _allocate_run_number(session, uuid.uuid4())
        assert result == 5

    @pytest.mark.asyncio
    async def test_non_postgres_path(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=mock_result)

        with patch("modulo.db.crud.run._get_dialect_name", new_callable=AsyncMock, return_value="sqlite"):
            from modulo.db.crud.run import _allocate_run_number

            result = await _allocate_run_number(session, uuid.uuid4())
        assert result == 3

    @pytest.mark.asyncio
    async def test_returns_1_on_none(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        with patch("modulo.db.crud.run._get_dialect_name", new_callable=AsyncMock, return_value="postgresql"):
            from modulo.db.crud.run import _allocate_run_number

            result = await _allocate_run_number(session, uuid.uuid4())
        assert result == 1


# ---------------------------------------------------------------------------
# _ensure_org_not_deleted (async, mocked)
# ---------------------------------------------------------------------------


class TestEnsureOrgNotDeleted:
    @pytest.mark.asyncio
    async def test_raises_when_org_missing(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        from modulo.core.exceptions import OrgDeletedError
        from modulo.db.crud.run import _ensure_org_not_deleted

        with pytest.raises(OrgDeletedError):
            await _ensure_org_not_deleted(session, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_raises_when_org_deleted(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "deleted"
        session.execute = AsyncMock(return_value=mock_result)

        from modulo.core.exceptions import OrgDeletedError
        from modulo.db.crud.run import _ensure_org_not_deleted

        with pytest.raises(OrgDeletedError):
            await _ensure_org_not_deleted(session, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_passes_when_org_active(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "active"
        session.execute = AsyncMock(return_value=mock_result)

        from modulo.db.crud.run import _ensure_org_not_deleted

        await _ensure_org_not_deleted(session, uuid.uuid4())
        # If no exception was raised, the org status check passed.
        session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# _read_guardrails_kill_switch (async, mocked)
# ---------------------------------------------------------------------------


class TestReadGuardrailsKillSwitch:
    @pytest.mark.asyncio
    async def test_returns_true_when_ks_set(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = True
        session.execute = AsyncMock(return_value=mock_result)

        from modulo.db.crud.run import _read_guardrails_kill_switch

        assert await _read_guardrails_kill_switch(session, uuid.uuid4()) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_ks_none(self):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        from modulo.db.crud.run import _read_guardrails_kill_switch

        assert await _read_guardrails_kill_switch(session, uuid.uuid4()) is False

    @pytest.mark.asyncio
    async def test_returns_false_on_db_error(self):
        from sqlalchemy.exc import SQLAlchemyError

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=SQLAlchemyError("db boom"))

        from modulo.db.crud.run import _read_guardrails_kill_switch

        assert await _read_guardrails_kill_switch(session, uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# _get_dialect_name (async, mocked)
# ---------------------------------------------------------------------------


class TestGetDialectName:
    @pytest.mark.asyncio
    async def test_returns_dialect_name(self):
        session = AsyncMock()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        session.get_bind = AsyncMock(return_value=bind)

        from modulo.db.crud.run import _get_dialect_name

        assert await _get_dialect_name(session) == "postgresql"


# ---------------------------------------------------------------------------
# _apply_run_claim_fields / _apply_run_error_fields / _apply_run_cost_fields / _apply_run_output_fields
# ---------------------------------------------------------------------------


class TestApplyRunClaimFields:
    def test_sets_started_at_when_running(self):
        run = SimpleNamespace(status="running", started_at=None, claimed_by=None, completed_at=None)
        update = SimpleNamespace(claimed_by=None)
        from modulo.db.crud.run import _apply_run_claim_fields

        _apply_run_claim_fields(run, "running", update)
        assert run.started_at is not None

    def test_does_not_overwrite_existing_started_at(self):
        existing = datetime.now(UTC)
        run = SimpleNamespace(status="running", started_at=existing, claimed_by=None, completed_at=None)
        update = SimpleNamespace(claimed_by=None)
        from modulo.db.crud.run import _apply_run_claim_fields

        _apply_run_claim_fields(run, "running", update)
        assert run.started_at == existing

    def test_sets_claimed_by(self):
        run = SimpleNamespace(status="running", started_at=None, claimed_by=None, completed_at=None)
        update = SimpleNamespace(claimed_by="executor-1")
        from modulo.db.crud.run import _apply_run_claim_fields

        _apply_run_claim_fields(run, "running", update)
        assert run.claimed_by == "executor-1"

    def test_sets_completed_at_for_terminal(self):
        run = SimpleNamespace(status="complete", started_at=None, claimed_by=None, completed_at=None)
        update = SimpleNamespace(claimed_by=None)
        from modulo.db.crud.run import _apply_run_claim_fields

        _apply_run_claim_fields(run, "complete", update)
        assert run.completed_at is not None


class TestApplyRunErrorFields:
    def test_sets_error_code(self):
        run = SimpleNamespace(error_code=None, error_detail=None)
        update = SimpleNamespace(clear_error_code=False, error_code="timeout", error_detail="timed out")
        from modulo.db.crud.run import _apply_run_error_fields

        _apply_run_error_fields(run, update)
        assert run.error_code == "timeout"
        assert run.error_detail == "timed out"

    def test_clears_error_code(self):
        run = SimpleNamespace(error_code="old", error_detail="old detail")
        update = SimpleNamespace(clear_error_code=True, error_code=None, error_detail=None)
        from modulo.db.crud.run import _apply_run_error_fields

        _apply_run_error_fields(run, update)
        assert run.error_code is None
        assert run.error_detail is None

    def test_no_change_when_none(self):
        run = SimpleNamespace(error_code="keep", error_detail="keep")
        update = SimpleNamespace(clear_error_code=False, error_code=None, error_detail=None)
        from modulo.db.crud.run import _apply_run_error_fields

        _apply_run_error_fields(run, update)
        assert run.error_code == "keep"


class TestApplyRunCostFields:
    def test_sets_total_tokens(self):
        run = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown="old")
        update = SimpleNamespace(total_tokens=100, total_cost_usd=None, cost_breakdown=SimpleNamespace)
        from modulo.db.crud.run import _apply_run_cost_fields

        _apply_run_cost_fields(run, update)
        assert run.total_tokens == 100

    def test_sets_cost_breakdown(self):
        run = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown="old")
        update = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown={"new": True})
        from modulo.db.crud.run import _apply_run_cost_fields

        _apply_run_cost_fields(run, update)
        assert run.cost_breakdown == {"new": True}

    def test_sentinel_leaves_cost_breakdown_alone(self):
        from modulo.db.crud.run import _COST_BREAKDOWN_SENTINEL

        run = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown="keep")
        update = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown=_COST_BREAKDOWN_SENTINEL)
        from modulo.db.crud.run import _apply_run_cost_fields

        _apply_run_cost_fields(run, update)
        assert run.cost_breakdown == "keep"

    def test_none_sets_null(self):
        run = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown="old")
        update = SimpleNamespace(total_tokens=None, total_cost_usd=None, cost_breakdown=None)
        from modulo.db.crud.run import _apply_run_cost_fields

        _apply_run_cost_fields(run, update)
        assert run.cost_breakdown is None


class TestApplyRunOutputFields:
    def test_sets_node_token_usage(self):
        run = SimpleNamespace(node_token_usage=None)
        update = SimpleNamespace(node_token_usage={"node1": 100})
        from modulo.db.crud.run import _apply_run_output_fields

        _apply_run_output_fields(run, update)
        assert run.node_token_usage == {"node1": 100}

    def test_none_leaves_alone(self):
        run = SimpleNamespace(node_token_usage={"keep": True})
        update = SimpleNamespace(node_token_usage=None)
        from modulo.db.crud.run import _apply_run_output_fields

        _apply_run_output_fields(run, update)
        assert run.node_token_usage == {"keep": True}
