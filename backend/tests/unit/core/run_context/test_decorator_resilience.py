"""Unit tests for decorator resilience: DB check failure, reserved key protection."""

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.pipeline_engine.decorator import (
    RunCancelledError,
    set_cancellation_check,
    cancellable_node,
)


class TestDbCheckFailure:
    """DB-backed cancellation check failure degrades gracefully."""

    async def test_db_check_exception_does_not_crash(self) -> None:
        async def failing_check() -> bool:
            msg = "DB connection lost"
            raise ConnectionError(msg)

        set_cancellation_check(failing_check)

        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await my_node(state)
        assert result == {"artifact": "done"}

    async def test_db_check_failure_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        async def failing_check() -> bool:
            msg = "timeout connecting to DB"
            raise TimeoutError(msg)

        set_cancellation_check(failing_check)

        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        with caplog.at_level(logging.WARNING):
            await my_node(state)

        records = [r for r in caplog.records if "cancellation_check_failed" in r.message]
        assert len(records) == 1
        assert "my_node" in records[0].node_name

    async def test_db_check_still_runs_when_no_exception(self) -> None:
        check_call_count = 0

        async def passing_check() -> bool:
            nonlocal check_call_count
            check_call_count += 1
            return False

        set_cancellation_check(passing_check)

        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        await my_node(state)
        assert check_call_count == 1

    async def test_db_check_true_still_cancels(self) -> None:
        async def cancelling_check() -> bool:
            return True

        set_cancellation_check(cancelling_check)

        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "should not reach"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        with pytest.raises(RunCancelledError, match="DB check"):
            await my_node(state)

    async def test_db_check_unset_is_noop(self) -> None:
        set_cancellation_check(None)

        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await my_node(state)
        assert result == {"artifact": "done"}


class TestReservedKeyProtection:
    """Context-setter agents cannot modify reserved keys."""

    async def test_cancelled_key_is_stripped(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"cancelled": True, "model_tier": "tier-2"}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await setter(state)
        # cancelled must be stripped, model_tier must pass through
        assert "cancelled" not in result["run_context"]
        assert result["run_context"]["model_tier"] == "tier-2"

    async def test_input_key_is_stripped(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"input": {"malicious": True}}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await setter(state)
        assert "input" not in result["run_context"]

    async def test_pipeline_default_autonomy_is_stripped(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"_pipeline_default_autonomy": "fully_autonomous"}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await setter(state)
        assert "_pipeline_default_autonomy" not in result["run_context"]

    async def test_write_log_key_is_stripped(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"_run_context_write_log": "should be stripped"}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await setter(state)
        assert "_run_context_write_log" not in result["run_context"]

    async def test_only_reserved_keys_returns_no_write_log(self) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"cancelled": True}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await setter(state)
        # No write-log entry should be created since no non-reserved keys were written
        assert "_run_context_write_log" not in result

    async def test_reserved_key_attempt_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"cancelled": True, "model_tier": "tier-2"}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        with caplog.at_level(logging.WARNING):
            await setter(state)

        records = [r for r in caplog.records if "reserved_key_attempt" in r.message]
        assert len(records) == 1
        assert "cancelled" in records[0].reserved_keys

    async def test_non_setter_reserved_keys_still_raises_violation(self) -> None:
        from modulo.core.pipeline_engine import ContextSetterViolationError

        @cancellable_node(role="agent")
        async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"cancelled": True}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        with pytest.raises(ContextSetterViolationError):
            await bad_node(state)
