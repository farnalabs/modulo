"""Unit tests for context-setter enforcement, audit warnings, and write-log.

Verifies:
- Only context_setter role nodes may write to run_context
- Violations emit a warning log entry
- Write-log records node name, timestamp, and written fields
- Write-log has last-write-wins semantics (entries append in order)
"""

import logging
from typing import Any

import pytest

from modulo.core.pipeline_engine import (
    ContextSetterViolationError,
    cancellable_node,
)
from modulo.core.pipeline_engine.decorator import _RUN_CONTEXT_WRITE_LOG_KEY

_LIVE_STATE: dict[str, Any] = {"run_context": {"cancelled": False}}


# ---------------------------------------------------------------------------
# Context-setter write log
# ---------------------------------------------------------------------------


class TestWriteLog:
    """Tests for the run_context write-log."""

    async def test_context_setter_creates_write_log(self):
        """A context_setter node should create a write-log entry."""

        @cancellable_node(role="context_setter")
        async def reviewer(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"model_tier": "tier-2"}}

        result = await reviewer(_LIVE_STATE)
        write_log = result.get(_RUN_CONTEXT_WRITE_LOG_KEY)
        assert write_log is not None
        assert len(write_log) == 1
        assert write_log[0]["node_name"] == "reviewer"
        assert write_log[0]["written_fields"] == ["model_tier"]

    async def test_write_log_has_timestamp(self):
        """Each write-log entry should have an ISO timestamp."""

        @cancellable_node(role="context_setter")
        async def writer(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"key": "value"}}

        result = await writer(_LIVE_STATE)
        entry = result[_RUN_CONTEXT_WRITE_LOG_KEY][0]
        assert "timestamp" in entry
        assert "T" in entry["timestamp"]  # ISO format

    async def test_write_log_records_written_fields(self):
        """Write-log should record exactly the fields that were written."""

        @cancellable_node(role="context_setter")
        async def multi_writer(state: dict[str, Any]) -> dict[str, Any]:
            return {
                "run_context": {
                    "model_tier": "tier-3",
                    "estimated_tokens": 5000,
                    "complexity_reason": "Complex multi-step analysis",
                }
            }

        result = await multi_writer(_LIVE_STATE)
        entry = result[_RUN_CONTEXT_WRITE_LOG_KEY][0]
        assert "model_tier" in entry["written_fields"]
        assert "estimated_tokens" in entry["written_fields"]
        assert "complexity_reason" in entry["written_fields"]

    async def test_multiple_setters_append_to_write_log(self):
        """Multiple context_setter nodes should append to the same write-log."""

        @cancellable_node(role="context_setter")
        async def setter_a(state: dict[str, Any]) -> dict[str, Any]:
            write_log = list(state.get(_RUN_CONTEXT_WRITE_LOG_KEY) or [])
            write_log.append(
                {
                    "node_name": "setter_a",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "written_fields": ["field_a"],
                }
            )
            return {
                "run_context": {"field_a": "value_a"},
                _RUN_CONTEXT_WRITE_LOG_KEY: write_log,
            }

        @cancellable_node(role="context_setter")
        async def setter_b(state: dict[str, Any]) -> dict[str, Any]:
            write_log = list(state.get(_RUN_CONTEXT_WRITE_LOG_KEY) or [])
            write_log.append(
                {
                    "node_name": "setter_b",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "written_fields": ["field_b"],
                }
            )
            return {
                "run_context": {"field_b": "value_b"},
                _RUN_CONTEXT_WRITE_LOG_KEY: write_log,
            }

        # Simulate sequential execution: setter_a then setter_b
        state = dict(_LIVE_STATE)
        state[_RUN_CONTEXT_WRITE_LOG_KEY] = []

        result_a = await setter_a(state)
        result_b = await setter_b({**state, **result_a})

        write_log = result_b[_RUN_CONTEXT_WRITE_LOG_KEY]
        assert len(write_log) == 2
        assert write_log[0]["node_name"] == "setter_a"
        assert write_log[1]["node_name"] == "setter_b"

    async def test_last_write_wins_semantics(self):
        """Later context-setter writes should win (last-write-wins)."""

        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"value": state.get("_new_value", "default")}}

        result = await setter({**_LIVE_STATE, "_new_value": "first"})
        assert result["run_context"]["value"] == "first"

        result = await setter({**_LIVE_STATE, "_new_value": "last"})
        assert result["run_context"]["value"] == "last"


# ---------------------------------------------------------------------------
# Audit warning on violations
# ---------------------------------------------------------------------------


class TestAuditWarning:
    """Tests that violations emit audit warnings."""

    async def test_violation_emits_warning(self, caplog):
        """A non-context-setter writing to run_context should log a warning."""

        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"secret": "data"}}

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ContextSetterViolationError):
                await bad_node(_LIVE_STATE)

        # Check that a warning was logged about the violation
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("run_context" in msg for msg in warning_messages)

    async def test_standard_node_no_violation_no_warning(self, caplog):
        """A standard node not writing to run_context should not produce warnings."""

        @cancellable_node(role="agent")
        async def good_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": {"result": "done"}}

        with caplog.at_level(logging.WARNING):
            result = await good_node(_LIVE_STATE)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0
        assert result["artifact"]["result"] == "done"

    async def test_violation_logs_attempted_fields(self, caplog):
        """The violation warning should include the attempted fields."""

        @cancellable_node(role="runner")
        async def runner_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"confidential": True}}

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ContextSetterViolationError):
                await runner_node(_LIVE_STATE)

        violation_records = [r for r in caplog.records if r.levelno == logging.WARNING and "violation" in r.message]
        assert len(violation_records) > 0  # At least one log entry

    async def test_context_setter_no_warning(self, caplog):
        """A context_setter writing to run_context should NOT produce warnings."""

        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"setting": "value"}}

        with caplog.at_level(logging.WARNING):
            result = await setter(_LIVE_STATE)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0
        assert result["run_context"]["setting"] == "value"

    async def test_node_with_no_role_cannot_write_context(self):
        """A node with no explicit role should not be able to write to run_context."""

        @cancellable_node()
        async def no_role_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"should_fail": True}}

        with pytest.raises(ContextSetterViolationError):
            await no_role_node(_LIVE_STATE)


# ---------------------------------------------------------------------------
# Write log is part of state (key existence)
# ---------------------------------------------------------------------------


class TestWriteLogKey:
    """Tests that the write-log key is properly defined and accessible."""

    def test_write_log_key_is_defined(self):
        """The write-log key constant should be defined."""
        from modulo.core.pipeline_engine.decorator import _RUN_CONTEXT_WRITE_LOG_KEY

        assert _RUN_CONTEXT_WRITE_LOG_KEY == "_run_context_write_log"

    async def test_write_log_key_present_in_result(self):
        """The write-log key should be present in the result from a context_setter."""

        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"x": 1}}

        result = await setter(_LIVE_STATE)
        assert _RUN_CONTEXT_WRITE_LOG_KEY in result
        assert len(result[_RUN_CONTEXT_WRITE_LOG_KEY]) == 1
