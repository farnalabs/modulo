"""Tests for pipeline execution core logic."""

import uuid
from datetime import UTC, datetime

import pytest

from modulo.core.pipeline_engine.executor import _graph_json_hash, _map_lg_event, _seed_state, _strip_asyncpg
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError


class TestGraphJsonHash:
    def test_consistent_hash(self) -> None:
        data = {"nodes": [{"id": "a"}], "edges": []}
        h1 = _graph_json_hash(data)
        h2 = _graph_json_hash(data)
        assert h1 == h2

    def test_different_input_different_hash(self) -> None:
        a = _graph_json_hash({"nodes": [{"id": "a"}]})
        b = _graph_json_hash({"nodes": [{"id": "b"}]})
        assert a != b

    def test_key_order_independence(self) -> None:
        h1 = _graph_json_hash({"a": 1, "b": 2})
        h2 = _graph_json_hash({"b": 2, "a": 1})
        assert h1 == h2


class TestMapLgEvent:
    def test_node_start_event(self) -> None:
        event = {"event": "on_chain_start", "name": "node-1"}
        result = _map_lg_event(event, uuid.uuid4(), {"node-1"})
        assert result is not None
        event_type, payload = result
        assert event_type == "node_started"
        assert payload["node_id"] == "node-1"

    def test_node_complete_event(self) -> None:
        event = {"event": "on_chain_end", "name": "node-1"}
        result = _map_lg_event(event, uuid.uuid4(), {"node-1"})
        assert result is not None
        event_type, payload = result
        assert event_type == "node_completed"
        assert payload["node_id"] == "node-1"

    def test_node_error_event(self) -> None:
        event = {"event": "on_chain_error", "name": "node-1", "data": {"error": "something broke"}}
        result = _map_lg_event(event, uuid.uuid4(), {"node-1"})
        assert result is not None
        event_type, payload = result
        assert event_type == "node_failed"
        assert "something broke" in payload["error"]

    def test_unknown_event_kind_returns_none(self) -> None:
        event = {"event": "on_custom_event", "name": "node-1"}
        result = _map_lg_event(event, uuid.uuid4(), {"node-1"})
        assert result is None

    def test_unknown_node_returns_none(self) -> None:
        event = {"event": "on_chain_start", "name": "unknown-node"}
        result = _map_lg_event(event, uuid.uuid4(), {"known-node"})
        assert result is None


class TestSeedState:
    def test_basic_seed(self) -> None:
        snapshot = _make_snapshot(run_context_defaults={"branch": "main"})
        state = _seed_state(snapshot, {"key": "value"})
        assert state["run_context"]["input"] == {"key": "value"}
        assert state["run_context"]["cancelled"] is False
        assert state["run_context"]["branch"] == "main"
        assert state["artifacts"] == []

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


class TestStripAsyncpg:
    def test_asyncpg_url(self) -> None:
        assert _strip_asyncpg("postgresql+asyncpg://user:pass@localhost/db") == "postgresql://user:pass@localhost/db"

    def test_psycopg_url(self) -> None:
        assert _strip_asyncpg("postgresql+psycopg://user:pass@localhost/db") == "postgresql://user:pass@localhost/db"

    def test_plain_url_unchanged(self) -> None:
        assert _strip_asyncpg("postgresql://user:pass@localhost/db") == "postgresql://user:pass@localhost/db"


class TestRunawayGuard:
    def test_no_limits_never_raises(self) -> None:
        guard = RunawayGuard()
        for _ in range(1000):
            guard.check_duration()
            guard.record_step()
            guard.record_tokens(9999)

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
