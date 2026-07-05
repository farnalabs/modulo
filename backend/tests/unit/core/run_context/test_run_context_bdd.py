"""Unit tests mirroring BDD scenarios for run_context seeding, write guard,
and audit behaviour.

These tests verify the same behaviours as run_context.feature but at the
function/unit level — no API layers, no DB mocking.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.core.pipeline_engine import (
    ContextSetterViolationError,
    cancellable_node,
)
from modulo.core.pipeline_engine.decorator import _RUN_CONTEXT_WRITE_LOG_KEY
from modulo.core.pipeline_engine.executor import _seed_state

_LIVE_STATE: dict[str, Any] = {"run_context": {"cancelled": False}}


# ===================================================================
#  Seeding
# ===================================================================


class TestSeedContext:
    """Run context is seeded at run start from snapshot defaults + input."""

    def test_seeds_defaults_and_input(self) -> None:
        snapshot = MagicMock()
        snapshot.run_context_defaults = {"env": "prod", "branch": "main"}
        snapshot.default_autonomy_level = None

        state = _seed_state(snapshot, {"task": "deploy"})

        rc = state["run_context"]
        assert rc["env"] == "prod"
        assert rc["branch"] == "main"
        assert rc["input"]["task"] == "deploy"
        assert rc["cancelled"] is False

    def test_seeded_context_has_empty_input_when_no_payload(self) -> None:
        snapshot = MagicMock()
        snapshot.run_context_defaults = {}
        snapshot.default_autonomy_level = None

        state = _seed_state(snapshot, {})

        assert state["run_context"]["input"] == {}
        assert state["run_context"]["cancelled"] is False

    def test_input_overrides_defaults(self) -> None:
        snapshot = MagicMock()
        snapshot.run_context_defaults = {"env": "prod"}
        snapshot.default_autonomy_level = None

        state = _seed_state(snapshot, {"env": "staging"})

        rc = state["run_context"]
        # input is separate from snapshot defaults — both coexist
        assert rc["env"] == "prod"
        assert rc["input"]["env"] == "staging"

    def test_seeded_state_has_artifacts_key(self) -> None:
        snapshot = MagicMock()
        snapshot.run_context_defaults = {}
        snapshot.default_autonomy_level = None

        state = _seed_state(snapshot, {})
        assert "artifacts" in state
        assert state["artifacts"] == []


# ===================================================================
#  Context-setter writes
# ===================================================================


class TestContextSetterWrite:
    """Designated context-setter nodes can write to run_context."""

    async def test_context_setter_can_write(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"model_tier": "tier-2"}}

        result = await setter(_LIVE_STATE)

        assert result["run_context"]["model_tier"] == "tier-2"

    async def test_context_setter_creates_write_log(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"model_tier": "tier-2"}}

        result = await setter(_LIVE_STATE)

        write_log = result.get(_RUN_CONTEXT_WRITE_LOG_KEY)
        assert write_log is not None
        assert len(write_log) == 1
        assert write_log[0]["node_name"] == "setter"
        assert write_log[0]["written_fields"] == ["model_tier"]

    async def test_context_setter_no_error(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"key": "val"}}

        result = await setter(_LIVE_STATE)
        assert result["run_context"]["key"] == "val"


# ===================================================================
#  Non-setter write rejection
# ===================================================================


class TestNonSetterWriteRejected:
    """Non-context-setter nodes writing to run_context raise an error."""

    async def test_agent_cannot_write_run_context(self) -> None:
        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"secret": "data"}}

        with pytest.raises(ContextSetterViolationError):
            await bad_node(_LIVE_STATE)

    async def test_runner_cannot_write_run_context(self) -> None:
        @cancellable_node(role="runner")
        async def runner_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"confidential": True}}

        with pytest.raises(ContextSetterViolationError):
            await runner_node(_LIVE_STATE)

    async def test_no_role_node_cannot_write_run_context(self) -> None:
        @cancellable_node()
        async def no_role_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"should_fail": True}}

        with pytest.raises(ContextSetterViolationError):
            await no_role_node(_LIVE_STATE)

    async def test_run_context_unchanged_on_violation(self) -> None:
        original: dict[str, Any] = {"run_context": {"cancelled": False}}

        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"secret": "data"}}

        with pytest.raises(ContextSetterViolationError):
            await bad_node(original)

        # State should be unchanged
        assert original["run_context"] == {"cancelled": False}


# ===================================================================
#  Last-write-wins semantics
# ===================================================================


class TestLastWriteWins:
    """Multiple writes by the same setter — last write wins."""

    async def test_later_write_overrides_earlier(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"model_tier": state["_tier"]}}

        # First write
        result1 = await setter({**_LIVE_STATE, "_tier": "tier-1"})
        assert result1["run_context"]["model_tier"] == "tier-1"

        # Second write — wins
        state2 = {**_LIVE_STATE, "_tier": "tier-3"}
        result2 = await setter(state2)
        assert result2["run_context"]["model_tier"] == "tier-3"

    async def test_write_log_appends_on_each_write(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"model_tier": state["_tier"]}}

        result1 = await setter({**_LIVE_STATE, "_tier": "tier-1"})
        log1 = result1[_RUN_CONTEXT_WRITE_LOG_KEY]
        assert len(log1) == 1

        # Simulate second write by feeding the accumulated state
        acc_state: dict[str, Any] = {**_LIVE_STATE, "_tier": "tier-3"}
        acc_state[_RUN_CONTEXT_WRITE_LOG_KEY] = list(log1)
        result2 = await setter(acc_state)
        log2 = result2[_RUN_CONTEXT_WRITE_LOG_KEY]
        assert len(log2) == 2


# ===================================================================
#  Multiple context-setters append
# ===================================================================


class TestMultipleSetters:
    """Multiple context-setter nodes append to the same write-log."""

    async def test_two_setters_append_to_log(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter_a(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"field_a": "value_a"}}

        @cancellable_node(role="context_setter")
        async def setter_b(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"field_b": "value_b"}}

        state: dict[str, Any] = dict(_LIVE_STATE)
        state[_RUN_CONTEXT_WRITE_LOG_KEY] = []

        result_a = await setter_a(state)
        result_b = await setter_b({**state, **result_a})

        log = result_b[_RUN_CONTEXT_WRITE_LOG_KEY]
        assert len(log) == 2
        assert log[0]["node_name"] == "setter_a"
        assert log[1]["node_name"] == "setter_b"
        assert log[0]["written_fields"] == ["field_a"]
        assert log[1]["written_fields"] == ["field_b"]


# ===================================================================
#  Context accessible by all nodes
# ===================================================================


class TestContextAccessible:
    """All nodes can read run_context regardless of role."""

    async def test_agent_can_read_run_context(self) -> None:
        state: dict[str, Any] = {
            "run_context": {"branch": "main", "cancelled": False}
        }

        @cancellable_node(role="agent")
        async def reader(state_in: dict[str, Any]) -> dict[str, Any]:
            branch = state_in["run_context"]["branch"]
            return {"artifact": {"branch": branch}}

        result = await reader(state)
        assert result["artifact"]["branch"] == "main"

    async def test_runner_can_read_run_context(self) -> None:
        state: dict[str, Any] = {"run_context": {"env": "prod", "cancelled": False}}

        @cancellable_node(role="runner")
        async def runner(state_in: dict[str, Any]) -> dict[str, Any]:
            env = state_in["run_context"].get("env", "unknown")
            return {"artifact": {"env": env}}

        result = await runner(state)
        assert result["artifact"]["env"] == "prod"

    async def test_context_setter_can_read_too(self) -> None:
        state: dict[str, Any] = {"run_context": {"env": "prod", "cancelled": False}}

        @cancellable_node(role="context_setter")
        async def setter(state_in: dict[str, Any]) -> dict[str, Any]:
            env = state_in["run_context"]["env"]
            return {"run_context": {"env": env}}

        result = await setter(state)
        assert result["run_context"]["env"] == "prod"


# ===================================================================
#  Artifact vs context separation
# ===================================================================


class TestArtifactContextSeparation:
    """run_context and artifacts are sibling keys in state."""

    def test_top_level_keys_are_siblings(self) -> None:
        snapshot = MagicMock()
        snapshot.run_context_defaults = {}
        snapshot.default_autonomy_level = None

        state = _seed_state(snapshot, {})

        assert "run_context" in state
        assert "artifacts" in state
        assert "run_context" not in state.get("artifacts", {})
        assert "artifacts" not in state.get("run_context", {})

    def test_artifacts_is_list_not_nested(self) -> None:
        snapshot = MagicMock()
        snapshot.run_context_defaults = {}
        snapshot.default_autonomy_level = None

        state = _seed_state(snapshot, {})
        assert isinstance(state["artifacts"], list)
        assert isinstance(state["run_context"], dict)

    async def test_node_write_to_artifact_keeps_separation(self) -> None:
        state: dict[str, Any] = {
            "run_context": {"cancelled": False},
            "artifacts": [],
        }

        @cancellable_node(role="agent")
        async def writer(state_in: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": {"result": "done"}}

        result = await writer(state)
        assert "artifact" in result
        assert "run_context" not in result


# ===================================================================
#  Audit warning on violation
# ===================================================================


class TestAuditWarning:
    """Non-setter writes generate audit warnings."""

    async def test_violation_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"secret": "data"}}

        with caplog.at_level(logging.WARNING), pytest.raises(ContextSetterViolationError):
            await bad_node(_LIVE_STATE)

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("violation" in msg for msg in warning_messages)

    async def test_warning_includes_node_name(self, caplog: pytest.LogCaptureFixture) -> None:
        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"data": True}}

        with caplog.at_level(logging.WARNING), pytest.raises(ContextSetterViolationError):
            await bad_node(_LIVE_STATE)

        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert records[0].node_name == "bad_node"

    async def test_warning_includes_attempted_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"secret_key": "classified"}}

        with caplog.at_level(logging.WARNING), pytest.raises(ContextSetterViolationError):
            await bad_node(_LIVE_STATE)

        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert "secret_key" in records[0].attempted_fields

    async def test_context_setter_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"setting": "value"}}

        with caplog.at_level(logging.WARNING):
            result = await setter(_LIVE_STATE)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0
        assert result["run_context"]["setting"] == "value"
