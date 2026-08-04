"""Direct unit tests for the run_sync helper in modulo.core.secrets_backend.

The AWS and Vault backends rely on run_sync for every external call, so its
contract (thread offload, *args/**kwargs forwarding, timeout enforcement,
exception propagation) deserves its own coverage instead of only the
indirect paths exercised through the backends.
"""

import asyncio
import threading

import pytest

from modulo.core.secrets_backend import DEFAULT_TIMEOUT, run_sync


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
        release = threading.Event()
        done = threading.Event()

        def slow() -> None:
            release.wait(5)
            done.set()

        with pytest.raises(TimeoutError):
            await run_sync(slow, timeout_seconds=0.05)
        # Let the worker thread unwind so it does not outlive the test.
        release.set()
        await asyncio.to_thread(done.wait, 5)

    async def test_cancellation_propagates(self) -> None:
        release = threading.Event()
        done = threading.Event()

        def long_running() -> None:
            release.wait(5)
            done.set()

        task = asyncio.create_task(run_sync(long_running))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Let the worker thread unwind so it does not outlive the test.
        release.set()
        await asyncio.to_thread(done.wait, 5)

    def test_default_timeout_is_sane(self) -> None:
        assert DEFAULT_TIMEOUT == 30.0
