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
from modulo.core.pipeline_engine.executor import _map_lg_event, _seed_state
from modulo.core.pipeline_engine.node_runner import make_node_fn
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
