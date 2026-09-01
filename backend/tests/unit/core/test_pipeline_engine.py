"""Tests for pipeline execution core logic."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.core.pipeline_engine.event_broker import RunEventBroker
from modulo.core.pipeline_engine.executor import _map_lg_event, _seed_state
from modulo.core.pipeline_engine.node_runner import (
    OutputSchemaValidationError,
    _validate_against_schema,
    make_node_fn,
)
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from modulo.model_backends.stub.backend import StubModelBackend


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
            def now(_tz=None):
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

        assert not broker.replay_since(1)

    def test_replay_empty_buffer_returns_empty(self) -> None:
        broker = RunEventBroker(run_id=uuid.uuid4())
        assert not broker.replay_since(0)

    def test_replay_requested_seq_older_than_buffer_returns_empty(self) -> None:
        from collections import deque

        from modulo.core.pipeline_engine.event_broker import RunEvent

        broker = RunEventBroker(run_id=uuid.uuid4())
        # Simulate a ring buffer where seq 1..4 were evicted and the oldest
        # retained event is seq 5 — replaying from seq 1 must return [].
        broker._buffer = deque([RunEvent(seq=5, event_type="node_started", run_id=uuid.uuid4(), payload={})])
        assert not broker.replay_since(1)


class TestOutputSchemaValidation:
    """Manual/agent node output validation raises a domain-specific error."""

    def test_missing_required_field_raises_domain_error(self) -> None:
        with pytest.raises(OutputSchemaValidationError, match="missing required field 'name'"):
            _validate_against_schema({"id": "1"}, {"required": ["name"]})

    def test_valid_output_passes(self) -> None:
        schema = {"required": ["name", "status"]}
        assert _validate_against_schema({"name": "x", "status": "done"}, schema) is None

    def test_error_is_a_value_error_subclass(self) -> None:
        with pytest.raises(ValueError, match="missing required field 'x'"):
            _validate_against_schema({}, {"required": ["x"]})

    def test_schema_validation_failure_maps_to_contract_schema(self) -> None:
        from modulo.core.pipeline_engine.error_codes import map_legacy_code

        assert map_legacy_code("schema_validation_failure") == "contract.schema"


class _AsyncStubAdapter:
    """Wrap the sync StubModelBackend so make_node_fn can ``await backend.invoke()``."""

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


class TestRunContextPromptTemplates:
    """PRD 8.18: ``run_context`` fields render as template variables.

    ``make_node_fn`` exposes ``run_context`` as a first-class Jinja variable
    (alongside ``state``), so ``{{ run_context.model_tier }}`` interpolates the
    seeded context key at node execution time.
    """

    async def test_run_context_key_renders_in_prompt_template(self) -> None:
        node_id = str(uuid.uuid4())
        backend_id = uuid.uuid4()
        node_def = {
            "id": node_id,
            "prompt_template": "model tier is {{ run_context.model_tier }}",
            "model_backend_id": str(backend_id),
        }
        node_fn = make_node_fn(node_def, role="agent")

        hub = ModelBackendHub()
        await hub.__aenter__()
        hub.register(
            backend_id,
            _AsyncStubAdapter(
                {
                    "model tier is tier-2": json.dumps({"ok": True}),
                }
            ),
        )
        set_model_backend_hub(hub)

        state: dict[str, Any] = {
            "run_context": {"model_tier": "tier-2", "input": {}},
            "artifacts": [],
        }

        try:
            result = await node_fn(state)
            assert "artifacts" in result
            assert len(result["artifacts"]) == 1
            assert result["artifacts"][0]["status"] == "completed"
            assert result["artifacts"][0]["output"] == {"ok": True}
        finally:
            set_model_backend_hub(None)
            await hub.__aexit__(None, None, None)

    async def test_context_scope_gates_state_run_context_view(self) -> None:
        """FAR-418 MAJOR-3 fix: ``context_scope`` must also bind the
        ``state.run_context`` view, not just the ``run_context`` template var.
        A template reading ``{{ state.run_context.<key> }}`` for a gated key must
        receive an empty value — otherwise a node could trivially bypass the
        need-to-know boundary.
        """
        node_id = str(uuid.uuid4())
        backend_id = uuid.uuid4()
        node_def = {
            "id": node_id,
            "prompt_template": "tier={{ run_context.model_tier }}|leak={{ state.run_context.secret }}",
            "model_backend_id": str(backend_id),
            "capability_scope": {"context_scope": ["model_tier"]},
        }
        node_fn = make_node_fn(node_def, role="agent")

        captured: dict[str, str] = {}

        class _CaptureAdapter:
            def __init__(self) -> None:
                self._inner = StubModelBackend({"tier=tier-2|leak=": json.dumps({"ok": True})})

            async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
                captured["prompt"] = messages[-1].content
                return await self._inner.ainvoke(messages, **kwargs)

            async def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
                yield await self._inner.ainvoke(messages, **kwargs)

            @property
            def backend_id(self) -> str:
                return "stub"

        hub = ModelBackendHub()
        await hub.__aenter__()
        hub.register(backend_id, _CaptureAdapter())
        set_model_backend_hub(hub)

        state: dict[str, Any] = {
            "run_context": {"model_tier": "tier-2", "secret": "X"},
            "artifacts": [],
        }

        try:
            result = await node_fn(state)
            assert result["artifacts"][0]["status"] == "completed"
        finally:
            set_model_backend_hub(None)
            await hub.__aexit__(None, None, None)

        # The scoped key must be visible, but the gated key must NOT leak via
        # the state.run_context view.
        assert "tier=tier-2" in captured["prompt"]
        assert "leak=X" not in captured["prompt"]
