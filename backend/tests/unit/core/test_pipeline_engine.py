"""Tests for pipeline execution core logic."""

import uuid
from datetime import UTC, datetime

import pytest

from modulo.core.pipeline_engine.event_broker import RunEventBroker
from modulo.core.pipeline_engine.executor import _map_lg_event, _seed_state
from modulo.core.pipeline_engine.node_runner import (
    OutputSchemaValidationError,
    _validate_against_schema,
)
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError


class TestMapLgEvent:
    def test_node_start_event(self) -> None:
        event = {"event": "on_chain_start", "name": "node-1"}
        result = _map_lg_event(event, {"node-1"})
        assert result is not None
        event_type, payload = result
        assert event_type == "node_started"
        assert payload["node_id"] == "node-1"

    def test_node_complete_event(self) -> None:
        event = {"event": "on_chain_end", "name": "node-1"}
        result = _map_lg_event(event, {"node-1"})
        assert result is not None
        event_type, payload = result
        assert event_type == "node_completed"
        assert payload["node_id"] == "node-1"

    def test_node_error_event(self) -> None:
        event = {"event": "on_chain_error", "name": "node-1", "data": {"error": "something broke"}}
        result = _map_lg_event(event, {"node-1"})
        assert result is not None
        event_type, payload = result
        assert event_type == "node_failed"
        assert "something broke" in payload["error"]

    def test_unknown_event_kind_returns_none(self) -> None:
        event = {"event": "on_custom_event", "name": "node-1"}
        result = _map_lg_event(event, {"node-1"})
        assert result is None

    def test_unknown_node_returns_none(self) -> None:
        event = {"event": "on_chain_start", "name": "unknown-node"}
        result = _map_lg_event(event, {"known-node"})
        assert result is None


class TestSeedState:
    def test_basic_seed(self) -> None:
        snapshot = _make_snapshot(run_context_defaults={"branch": "main"})
        state = _seed_state(snapshot, {"key": "value"})
        assert state["run_context"]["input"] == {"key": "value"}
        assert state["run_context"]["cancelled"] is False
        assert state["run_context"]["branch"] == "main"
        assert not state["artifacts"]

    def test_feedback_correction_is_promoted(self) -> None:
        snapshot = _make_snapshot(run_context_defaults={})
        state = _seed_state(snapshot, {"_feedback_correction": {"reason": "bad output"}, "data": "ok"})
        assert "feedback_correction" in state["run_context"]
        assert state["run_context"]["feedback_correction"] == {"reason": "bad output"}
        assert "_feedback_correction" not in state["run_context"]["input"]

    def test_autonomy_level_from_snapshot(self) -> None:
        snapshot = _make_snapshot(run_context_defaults={}, default_autonomy_level="fully_autonomous")
        state = _seed_state(snapshot, {})
        assert state["run_context"]["_pipeline_default_autonomy"] == "fully_autonomous"

    def test_no_autonomy_when_snapshot_default_is_none(self) -> None:
        snapshot = _make_snapshot(run_context_defaults={}, default_autonomy_level=None)
        state = _seed_state(snapshot, {})
        assert "_pipeline_default_autonomy" not in state["run_context"]


class TestRunawayGuard:
    def test_no_limits_never_raises(self) -> None:
        guard = RunawayGuard()
        for _ in range(1000):
            guard.check_duration()
            guard.record_step()
            guard.record_tokens(9999)
        # no limits configured — counters accumulate freely without raising
        assert guard._step_count == 1000
        assert guard._token_count == 1000 * 9999

    def test_max_steps_triggers(self) -> None:
        guard = RunawayGuard(max_steps=3)
        guard.record_step()
        guard.record_step()
        guard.record_step()
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_step()
        assert excinfo.value.guard == "max_steps"
        assert excinfo.value.current == 4

    def test_max_steps_edge_not_exceeded(self) -> None:
        guard = RunawayGuard(max_steps=3)
        guard.record_step()
        guard.record_step()
        guard.record_step()
        # the limit is exclusive — exactly max_steps steps must not raise
        assert guard._step_count == 3

    def test_token_budget_triggers(self) -> None:
        guard = RunawayGuard(token_budget=100)
        guard.record_tokens(60)
        guard.record_tokens(30)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_tokens(20)
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 110

    def test_token_budget_edge_not_exceeded(self) -> None:
        guard = RunawayGuard(token_budget=100)
        guard.record_tokens(50)
        guard.record_tokens(50)
        # the limit is exclusive — exactly reaching the budget must not raise
        assert guard._token_count == 100

    def test_max_duration_triggers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

        class _FakeDatetime:
            @staticmethod
            def now(tz=None):
                return fake_time

        monkeypatch.setattr("modulo.core.pipeline_engine.runaway_protection.datetime", _FakeDatetime)
        guard = RunawayGuard(max_duration_seconds=10)
        fake_time = datetime(2025, 1, 1, 0, 1, 0, tzinfo=UTC)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.check_duration()
        assert excinfo.value.guard == "max_duration"


def _make_snapshot(
    run_context_defaults: dict | None = None,
    default_autonomy_level: str | None = "manual_approval",
) -> object:
    """Helper to create a minimal snapshot-like object."""
    from types import SimpleNamespace

    return SimpleNamespace(
        run_context_defaults=run_context_defaults or {},
        default_autonomy_level=default_autonomy_level,
    )


class TestRunEventBrokerReplay:
    """WebSocket reconnect replay — ring-buffer ``replay_since`` coverage.

    `replay_since()` is the reconnect-replay mechanism (100-event ring buffer
    per run). Previously implemented but untested.
    """

    def test_replay_returns_events_after_seq(self) -> None:
        broker = RunEventBroker(run_id=uuid.uuid4())
        for i in range(3):
            broker.publish(f"event_{i}", {"i": i})

        replayed = broker.replay_since(1)

        assert [e.event_type for e in replayed] == ["event_1", "event_2"]

    def test_replay_with_zero_seq_returns_all(self) -> None:
        broker = RunEventBroker(run_id=uuid.uuid4())
        broker.publish("node_started", {})
        broker.publish("node_completed", {})

        replayed = broker.replay_since(0)

        assert [e.event_type for e in replayed] == ["node_started", "node_completed"]

    def test_replay_after_latest_returns_empty(self) -> None:
        broker = RunEventBroker(run_id=uuid.uuid4())
        broker.publish("run_completed", {})

        assert broker.replay_since(1) == []

    def test_replay_empty_buffer_returns_empty(self) -> None:
        broker = RunEventBroker(run_id=uuid.uuid4())
        assert broker.replay_since(0) == []

    def test_replay_requested_seq_older_than_buffer_returns_empty(self) -> None:
        from collections import deque

        from modulo.core.pipeline_engine.event_broker import RunEvent

        broker = RunEventBroker(run_id=uuid.uuid4())
        # Simulate a ring buffer where seq 1..4 were evicted and the oldest
        # retained event is seq 5 — replaying from seq 1 must return [].
        broker._buffer = deque([RunEvent(seq=5, event_type="node_started", run_id=uuid.uuid4(), payload={})])
        assert broker.replay_since(1) == []


class TestOutputSchemaValidation:
    """Manual/agent node output validation raises a domain-specific error."""

    def test_missing_required_field_raises_domain_error(self) -> None:
        with pytest.raises(OutputSchemaValidationError, match="missing required field 'name'"):
            _validate_against_schema({"id": "1"}, {"required": ["name"]})

    def test_valid_output_passes(self) -> None:
        schema = {"required": ["name", "status"]}
        _validate_against_schema({"name": "x", "status": "done"}, schema)

    def test_error_is_a_value_error_subclass(self) -> None:
        with pytest.raises(ValueError):
            _validate_against_schema({}, {"required": ["x"]})

    def test_schema_validation_failure_maps_to_contract_schema(self) -> None:
        from modulo.core.pipeline_engine.error_codes import map_legacy_code

        assert map_legacy_code("schema_validation_failure") == "contract.schema"
