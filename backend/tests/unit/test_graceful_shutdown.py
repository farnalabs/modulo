"""Tests for the graceful shutdown manager and middleware."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from modulo.core.graceful_shutdown import ShutdownManager, ShutdownMiddleware


@pytest.fixture
def manager() -> ShutdownManager:
    return ShutdownManager(timeout=5.0)


class TestShutdownManager:
    async def test_shutdown_with_no_registered_resources(self, manager: ShutdownManager) -> None:
        await manager.shutdown()
        assert manager.is_shutting_down

    async def test_shutdown_calls_cleanup_in_reverse_order(self) -> None:
        calls: list[str] = []

        async def cleanup_a() -> None:
            calls.append("a")

        async def cleanup_b() -> None:
            calls.append("b")

        m = ShutdownManager(timeout=5.0)
        m.register("a", cleanup_a)
        m.register("b", cleanup_b)
        await m.shutdown()
        assert calls == ["b", "a"]

    async def test_shutdown_continues_on_failure(self, manager: ShutdownManager) -> None:
        async def failing() -> None:
            raise RuntimeError("cleanup failed")

        ok_fn = AsyncMock()
        manager.register("failing", failing)
        manager.register("ok", ok_fn)
        await manager.shutdown()
        ok_fn.assert_awaited_once()

    async def test_drains_active_requests(self) -> None:
        manager = ShutdownManager(timeout=5.0)
        manager.request_started()
        manager.request_started()

        async def drain() -> None:
            await asyncio.sleep(0.05)
            manager.request_finished()
            manager.request_finished()

        async def shutdown() -> None:
            await manager.shutdown()

        _, result = await asyncio.gather(drain(), shutdown())
        assert result is None

    async def test_drain_timeout_logs_warning(self, manager: ShutdownManager) -> None:
        manager.request_started()
        manager.timeout = 0.05
        await manager.shutdown()
        assert manager.is_shutting_down

    async def test_shutdown_sets_flag(self, manager: ShutdownManager) -> None:
        assert not manager.is_shutting_down
        await manager.shutdown()
        assert manager.is_shutting_down

    async def test_double_shutdown_is_safe(self, manager: ShutdownManager) -> None:
        await manager.shutdown()
        await manager.shutdown()
        assert manager.is_shutting_down

    async def test_register_same_name_twice(self) -> None:
        calls: list[str] = []

        async def cleanup_a() -> None:
            calls.append("a")

        async def cleanup_b() -> None:
            calls.append("b")

        m = ShutdownManager(timeout=5.0)
        m.register("resource", cleanup_a)
        m.register("resource", cleanup_b)
        await m.shutdown()
        assert calls == ["b", "a"]

    async def test_request_finished_without_started(self, manager: ShutdownManager) -> None:
        manager.request_finished()
        assert manager._active_requests == -1

    async def test_multiple_rapid_request_cycles(self, manager: ShutdownManager) -> None:
        for _ in range(10):
            manager.request_started()
            manager.request_finished()
        assert manager._active_requests == 0
        await manager.shutdown()


class TestShutdownMiddleware:
    async def test_tracks_requests(self) -> None:
        manager = ShutdownManager(timeout=5.0)
        middleware = ShutdownMiddleware(app=None, manager=manager)  # type: ignore[arg-type]

        async def send(_: object) -> None:
            pass

        async def receive() -> object:
            return {"type": "http.request"}

        inner_app = AsyncMock()
        middleware.app = inner_app

        # Capture the active-request count while the inner app is running.
        observed: list[int] = []

        async def observe(*_: object) -> None:
            observed.append(manager._active_requests)

        inner_app.side_effect = observe

        await middleware({"type": "http"}, receive, send)
        inner_app.assert_awaited_once()
        assert observed == [1], "middleware did not track the in-flight request"
        assert manager._active_requests == 0, "request was not released after completion"

    async def test_skips_non_http_scopes(self) -> None:
        manager = ShutdownManager(timeout=5.0)
        inner = AsyncMock()
        middleware = ShutdownMiddleware(app=inner, manager=manager)

        async def send(_: object) -> None:
            pass

        async def receive() -> object:
            return {"type": "websocket.receive"}

        await middleware({"type": "websocket"}, receive, send)
        inner.assert_awaited_once()
        assert manager._active_requests == 0

    async def test_decrements_on_error(self) -> None:
        manager = ShutdownManager(timeout=5.0)
        inner = AsyncMock(side_effect=RuntimeError("boom"))

        async def send(_: object) -> None:
            pass

        async def receive() -> object:
            return {"type": "http.request"}

        middleware = ShutdownMiddleware(app=inner, manager=manager)

        with pytest.raises(RuntimeError, match="boom"):
            await middleware({"type": "http"}, receive, send)

        assert manager._active_requests == 0
