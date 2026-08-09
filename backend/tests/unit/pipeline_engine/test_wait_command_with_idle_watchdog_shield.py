"""Unit tests for _wait_command_with_idle_watchdog's E2B events-task shield.

The E2B SDK's ``handle.wait()`` awaits a long-lived internal task created at
handle construction (``self._wait = asyncio.create_task(self._handle_events())``).
If a poll-slice timeout cancels that internal task, the next slice re-awaits a
dead task and immediately raises ``CancelledError`` with ``cancelling()==0``,
which LangGraph surfaces as ``NodeCancelledError`` — every sandbox run would
fail ~one tick in. The helper must shield the wait so slice timeouts cancel only
the shield (this was reproduced against a real E2B sandbox).
"""

import asyncio
import time

import pytest

from modulo.core.pipeline_engine.node_runner import _wait_command_with_idle_watchdog


class _IdleWatchdogHandle:
    """Fake E2B command handle mirroring the SDK's internal-events-task shape.

    ``wait()`` returns the long-lived ``self._events`` task directly, exactly as
    the E2B SDK does — so an unwrapped ``asyncio.wait_for`` slice timeout
    cancels that task and the next slice re-awaits a dead task.
    """

    def __init__(self) -> None:
        self._done = asyncio.Event()
        self._result: object = None
        self.kill_called = False
        self._events = asyncio.create_task(self._run())  # nosemgrep: create-task-without-guard

    async def _run(self) -> object:
        await self._done.wait()
        return self._result

    def complete(self, result: object) -> None:
        self._result = result
        self._done.set()

    def wait(self) -> asyncio.Task:
        return self._events

    async def kill(self) -> None:
        self.kill_called = True


def _fresh_activity() -> float:
    """Always-fresh last_activity: the idle watchdog must never fire."""
    return time.monotonic()


async def test_shield_survives_multiple_slice_timeouts():
    """The command's events task survives several slice timeouts and the helper
    returns the command result once the command completes.

    Pre-fix, the first slice timeout cancelled the E2B events task and the
    second slice re-awaited a dead task -> CancelledError on every sandbox run.
    """
    handle = _IdleWatchdogHandle()

    ticks = 0

    async def _on_tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            handle.complete("command-result")

    result = await _wait_command_with_idle_watchdog(
        handle,
        total_timeout=10.0,
        idle_timeout=5.0,
        last_activity=_fresh_activity,
        on_tick=_on_tick,
        tick_interval=0.05,
    )

    assert result == "command-result"
    assert ticks >= 3
    assert handle.kill_called is False


async def test_idle_stall_kills_and_raises_stalled():
    """A command that never completes and never produces activity is killed by
    the idle watchdog and raises TimeoutError containing 'stalled'."""
    handle = _IdleWatchdogHandle()

    stalled_at = time.monotonic()

    with pytest.raises(TimeoutError, match="stalled"):
        await _wait_command_with_idle_watchdog(
            handle,
            total_timeout=10.0,
            idle_timeout=0.2,
            last_activity=lambda: stalled_at,
            tick_interval=0.05,
        )

    assert handle.kill_called is True


async def test_total_timeout_raises_and_leaves_events_task_uncancelled():
    """When the total timeout elapses the helper raises TimeoutError and the
    E2B events task survives (per-slice shield cancels only the shield, never
    the long-lived events task the next slice would re-await)."""
    handle = _IdleWatchdogHandle()

    with pytest.raises(TimeoutError, match="total timeout"):
        await _wait_command_with_idle_watchdog(
            handle,
            total_timeout=0.2,
            idle_timeout=5.0,
            last_activity=_fresh_activity,
            tick_interval=0.05,
        )

    assert handle.kill_called is False
    assert handle.wait().cancelled() is False
