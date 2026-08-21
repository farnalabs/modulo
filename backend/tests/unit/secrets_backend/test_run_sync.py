"""Direct unit tests for the run_sync helper in modulo.core.secrets_backend.

The AWS and Vault backends rely on run_sync for every external call, so its
contract (thread offload, *args/**kwargs forwarding, timeout enforcement,
exception propagation) deserves its own coverage instead of only the
indirect paths exercised through the backends.
"""

import asyncio
import threading
from collections.abc import Callable

import pytest

from modulo.core.secrets_backend import DEFAULT_TIMEOUT, run_sync


def _blocking_callable() -> tuple[Callable[[], None], threading.Event, threading.Event]:
    """Return a callable that blocks until released, plus the release/done events.

    The underlying worker thread keeps running even after ``run_sync`` times out
    or is cancelled, so tests must release it and wait for it to finish.
    """
    release = threading.Event()
    done = threading.Event()

    def block_until_released() -> None:
        release.wait(5)
        done.set()

    return block_until_released, release, done


async def _unwind_worker_thread(release: threading.Event, done: threading.Event) -> None:
    """Let a leaked worker thread finish so it does not outlive the test."""
    release.set()
    await asyncio.to_thread(done.wait, 5)


class TestRunSync:
    async def test_returns_callable_result(self) -> None:
        def add(a: int, b: int) -> int:
            return a + b

        result = await run_sync(add, 2, 3)
        assert result == 5

    async def test_forwards_keyword_arguments(self) -> None:
        def multiply(*, a: int, b: int) -> int:
            return a * b

        result = await run_sync(multiply, a=4, b=6)
        assert result == 24

    async def test_runs_in_worker_thread(self) -> None:
        caller_thread = threading.get_ident()

        def current_thread() -> int:
            return threading.get_ident()

        worker_thread = await run_sync(current_thread)
        assert worker_thread != caller_thread

    async def test_propagates_exception(self) -> None:
        def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await run_sync(boom)

    async def test_timeout_raises_timeout_error(self) -> None:
        block_until_released, release, done = _blocking_callable()

        with pytest.raises(TimeoutError):
            await run_sync(block_until_released, timeout_seconds=0.05)
        await _unwind_worker_thread(release, done)

    async def test_cancellation_propagates(self) -> None:
        block_until_released, release, done = _blocking_callable()

        task = asyncio.create_task(run_sync(block_until_released))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _unwind_worker_thread(release, done)

    def test_default_timeout_is_sane(self) -> None:
        # Intentional invariant: run_sync must have a non-zero default timeout so
        # backend calls cannot block forever. 30.0 is a deliberate drift-guard.
        assert DEFAULT_TIMEOUT == 30.0
