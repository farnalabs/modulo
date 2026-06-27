"""Unit tests for @cancellable_node decorator."""

import asyncio
from typing import Any

import pytest

from modulo.core.pipeline_engine import (
    ContextSetterViolationError,
    RunCancelledError,
    cancellable_node,
    set_cancellation_check,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_STATE: dict[str, Any] = {}
_LIVE_STATE: dict[str, Any] = {"run_context": {"cancelled": False}}
_CANCELLED_STATE: dict[str, Any] = {"run_context": {"cancelled": True}}


async def _make_node(
    return_value: dict[str, Any] | None = None,
    *,
    delay: float = 0.0,
    role: str | None = None,
    node_timeout: float | None = None,
) -> Any:
    """Return a decorated node that resolves to return_value after delay seconds."""
    rv = return_value or {}

    @cancellable_node(timeout=node_timeout, role=role)
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        if delay:
            await asyncio.sleep(delay)
        return rv

    return node


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_normal_execution_returns_result():
    node = await _make_node({"output": "hello"})
    result = await node(_LIVE_STATE)
    assert result == {"output": "hello"}


async def test_node_with_no_run_context_in_state():
    node = await _make_node({"x": 1})
    result = await node(_EMPTY_STATE)
    assert result == {"x": 1}


async def test_node_with_none_run_context_in_state():
    node = await _make_node({"x": 1})
    result = await node({"run_context": None})
    assert result == {"x": 1}


async def test_empty_result_is_allowed():
    node = await _make_node({})
    result = await node(_LIVE_STATE)
    assert result == {}


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancelled_state_raises_before_node_runs():
    call_count = 0

    @cancellable_node()
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {}

    with pytest.raises(RunCancelledError):
        await node(_CANCELLED_STATE)

    assert call_count == 0, "Node body must not execute when cancelled"


async def test_not_cancelled_state_runs_normally():
    node = await _make_node({"ok": True})
    result = await node(_LIVE_STATE)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


async def test_timeout_raises_when_exceeded():
    @cancellable_node(timeout=0.05)
    async def slow_node(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10.0)
        return {}

    with pytest.raises(asyncio.TimeoutError):
        await slow_node(_LIVE_STATE)


async def test_no_timeout_runs_without_limit():
    node = await _make_node({"done": True}, delay=0.05)
    result = await node(_LIVE_STATE)
    assert result["done"] is True


async def test_timeout_not_exceeded_returns_result():
    node = await _make_node({"x": 42}, delay=0.01, node_timeout=5.0)
    result = await node(_LIVE_STATE)
    assert result["x"] == 42


# ---------------------------------------------------------------------------
# Context-setter guard
# ---------------------------------------------------------------------------


async def test_non_context_setter_writing_run_context_raises():
    @cancellable_node(role=None)
    async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"run_context": {"some": "data"}}

    with pytest.raises(ContextSetterViolationError):
        await bad_node(_LIVE_STATE)


async def test_context_setter_may_write_run_context():
    @cancellable_node(role="context_setter")
    async def ctx_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"run_context": {"seeded": True}}

    result = await ctx_node(_LIVE_STATE)
    assert result["run_context"]["seeded"] is True


async def test_non_context_setter_may_write_other_keys():
    @cancellable_node(role=None)
    async def agent_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"artifact": {"result": "done"}}

    result = await agent_node(_LIVE_STATE)
    assert result["artifact"]["result"] == "done"


async def test_explicit_non_setter_role_also_blocked():
    @cancellable_node(role="agent")
    async def agent_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"run_context": {"bad": True}}

    with pytest.raises(ContextSetterViolationError, match="context_setter"):
        await agent_node(_LIVE_STATE)


# ---------------------------------------------------------------------------
# Decorator preserves function metadata
# ---------------------------------------------------------------------------


def test_wraps_preserves_name():
    @cancellable_node()
    async def my_named_node(state: dict[str, Any]) -> dict[str, Any]:
        return {}

    assert my_named_node.__name__ == "my_named_node"


# ---------------------------------------------------------------------------
# DB-backed cancellation check (set_cancellation_check via ContextVar)
# ---------------------------------------------------------------------------


async def test_db_check_raises_when_cancelled():
    call_count = [0]

    async def _mock_db_check() -> bool:
        call_count[0] += 1
        return True

    set_cancellation_check(_mock_db_check)
    try:

        @cancellable_node()
        async def node(state: dict[str, Any]) -> dict[str, Any]:
            return {}

        with pytest.raises(RunCancelledError, match="DB check"):
            await node(_LIVE_STATE)
    finally:
        set_cancellation_check(None)

    assert call_count[0] == 1, "DB check must be called"


async def test_db_check_not_called_when_state_already_cancelled():
    db_call_count = [0]

    async def _mock_db_check() -> bool:
        db_call_count[0] += 1
        return True

    set_cancellation_check(_mock_db_check)
    try:

        @cancellable_node()
        async def node(state: dict[str, Any]) -> dict[str, Any]:
            return {}

        with pytest.raises(RunCancelledError):
            await node(_CANCELLED_STATE)
    finally:
        set_cancellation_check(None)

    assert db_call_count[0] == 0, "DB check must be skipped when state already cancelled"


async def test_node_runs_when_db_check_returns_false():
    call_count = [0]
    db_call_count = [0]

    async def _mock_db_check() -> bool:
        db_call_count[0] += 1
        return False

    set_cancellation_check(_mock_db_check)
    try:

        @cancellable_node()
        async def node(state: dict[str, Any]) -> dict[str, Any]:
            call_count[0] += 1
            return {"done": True}

        result = await node(_LIVE_STATE)
    finally:
        set_cancellation_check(None)

    assert result["done"] is True
    assert call_count[0] == 1, "Node body must execute"
    assert db_call_count[0] == 1, "DB check must be called"


async def test_db_check_cleared_after_finally():
    """set_cancellation_check(None) must clear the hook so it doesn't leak."""
    call_count = [0]

    async def _mock_db_check() -> bool:
        call_count[0] += 1
        return True

    set_cancellation_check(_mock_db_check)
    set_cancellation_check(None)

    @cancellable_node()
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    result = await node(_LIVE_STATE)
    assert result["ok"] is True
    assert call_count[0] == 0, "DB check must not be called after cleared"


async def test_db_check_not_called_when_not_set():
    @cancellable_node()
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    result = await node(_LIVE_STATE)
    assert result["ok"] is True


async def test_context_var_isolation():
    """Simulate two concurrent tasks with different DB check values."""

    results: list[str] = []

    async def task_a():
        async def _check_a() -> bool:
            return True

        set_cancellation_check(_check_a)
        try:

            @cancellable_node()
            async def node_a(state: dict[str, Any]) -> dict[str, Any]:
                return {}

            with pytest.raises(RunCancelledError, match="DB check"):
                await node_a(_LIVE_STATE)
            results.append("a_cancelled")
        finally:
            set_cancellation_check(None)

    async def task_b():
        async def _check_b() -> bool:
            return False

        set_cancellation_check(_check_b)
        try:

            @cancellable_node()
            async def node_b(state: dict[str, Any]) -> dict[str, Any]:
                return {"from": "b"}

            result = await node_b(_LIVE_STATE)
            assert result["from"] == "b"
            results.append("b_ran")
        finally:
            set_cancellation_check(None)

    await asyncio.gather(task_a(), task_b())
    assert results == ["a_cancelled", "b_ran"] or results == ["b_ran", "a_cancelled"]
