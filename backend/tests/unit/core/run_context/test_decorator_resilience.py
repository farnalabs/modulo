"""Unit tests for decorator resilience: DB check failure, reserved key protection."""

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.core.pipeline_engine.decorator import (
    RunCancelledError,
    cancellable_node,
    get_connector_hub,
    get_model_backend_hub,
    set_cancellation_check,
    set_connector_hub,
    set_model_backend_hub,
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

    @pytest.mark.parametrize(
        ("reserved_key", "value"),
        [
            ("cancelled", True),
            ("input", {"malicious": True}),
            ("_pipeline_default_autonomy", "fully_autonomous"),
            ("_run_context_write_log", "should be stripped"),
        ],
    )
    async def test_reserved_key_is_stripped(self, reserved_key: str, value: Any) -> None:
        @cancellable_node(role="context_setter")
        async def setter(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {reserved_key: value}}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await setter(state)
        assert reserved_key not in result["run_context"]

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


class TestHubContextVars:
    """Per-run ModelBackendHub / ConnectorHub ContextVar accessors."""

    async def test_connector_hub_defaults_to_none(self) -> None:
        set_connector_hub(None)
        assert get_connector_hub() is None

    async def test_connector_hub_roundtrip(self) -> None:
        hub = MagicMock()
        set_connector_hub(hub)
        try:
            assert get_connector_hub() is hub
        finally:
            set_connector_hub(None)
        assert get_connector_hub() is None

    async def test_model_backend_hub_defaults_to_none(self) -> None:
        set_model_backend_hub(None)
        assert get_model_backend_hub() is None

    async def test_model_backend_hub_roundtrip(self) -> None:
        hub = MagicMock()
        set_model_backend_hub(hub)
        try:
            assert get_model_backend_hub() is hub
        finally:
            set_model_backend_hub(None)
        assert get_model_backend_hub() is None


class TestCancellationEdges:
    """State fast-path cancellation and CancelledError propagation."""

    async def test_state_cancelled_raises_before_node_runs(self) -> None:
        call_count = 0

        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": True}}
        with pytest.raises(RunCancelledError, match="before node"):
            await my_node(state)
        assert call_count == 0

    async def test_state_missing_cancelled_key_runs_node(self) -> None:
        @cancellable_node(role="agent")
        async def my_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "done"}

        state: dict[str, Any] = {}
        result = await my_node(state)
        assert result == {"artifact": "done"}

    async def test_db_check_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError from the DB check must NOT be swallowed."""

        async def cancelling_check() -> bool:
            msg = "outer task cancelled"
            raise asyncio.CancelledError(msg)

        set_cancellation_check(cancelling_check)
        try:

            @cancellable_node(role="agent")
            async def my_node(state: dict[str, Any]) -> dict[str, Any]:
                return {"artifact": "done"}

            state: dict[str, Any] = {"run_context": {"cancelled": False}}
            with pytest.raises(asyncio.CancelledError):
                await my_node(state)
        finally:
            set_cancellation_check(None)


class TestTimeout:
    """Per-node timeout wrapping."""

    async def test_timeout_exceeded_raises(self) -> None:
        @cancellable_node(role="agent", timeout=0.01)
        async def slow_node(state: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(10.0)
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        with pytest.raises(TimeoutError, match=r"exceeded 0.01s timeout"):
            await slow_node(state)

    async def test_timeout_not_exceeded_returns_result(self) -> None:
        @cancellable_node(role="agent", timeout=5.0)
        async def quick_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifact": "done"}

        state: dict[str, Any] = {"run_context": {"cancelled": False}}
        result = await quick_node(state)
        assert result == {"artifact": "done"}
