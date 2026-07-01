"""Graceful shutdown manager for the Modulo FastAPI application.

Provides:
- ``ShutdownManager`` — coordinates draining in-flight requests and
  shutting down registered resources with a configurable timeout.
- ``ShutdownMiddleware`` — low-level ASGI middleware that tracks active
  requests so the manager can drain them before closing resources.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class ShutdownManager:
    """Coordinates graceful shutdown of application resources.

    Register resources with ``register(name, cleanup_fn)`` during startup.
    Call ``shutdown()`` during shutdown to drain requests and clean up
    in reverse registration order.

    Usage::

        manager = ShutdownManager(timeout=30.0)
        manager.register("db", db_engine.dispose)
        # ... during lifespan shutdown ...
        await manager.shutdown()
    """

    timeout: float = 30.0
    _shutting_down: bool = False
    _active_requests: int = 0
    _resources: list[tuple[str, Callable[[], Awaitable[None]]]] = field(default_factory=list)
    _idle_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def register(self, name: str, cleanup: Callable[[], Awaitable[None]]) -> None:
        """Register a resource to be cleaned up during shutdown."""
        self._resources.append((name, cleanup))

    def request_started(self) -> None:
        self._active_requests += 1

    def request_finished(self) -> None:
        self._active_requests -= 1
        self._idle_event.set()

    async def shutdown(self) -> None:
        """Execute the full shutdown sequence with timeout."""
        self._shutting_down = True
        _log.info("shutdown.beginning")

        # Phase 1: Drain in-flight requests
        await self._drain_requests()

        # Phase 2: Shut down registered resources in reverse order
        for name, cleanup in reversed(self._resources):
            _log.info("shutdown.closing", extra={"resource": name})
            try:
                await cleanup()
                _log.info("shutdown.closed", extra={"resource": name})
            except Exception:
                _log.exception("shutdown.failed", extra={"resource": name})

        _log.info("shutdown.complete")

    async def _drain_requests(self) -> None:
        if self._active_requests <= 0:
            return
        _log.info(
            "shutdown.draining_requests",
            extra={"active": self._active_requests, "timeout": self.timeout},
        )
        try:
            await asyncio.wait_for(self._wait_for_idle(), timeout=self.timeout)
            _log.info("shutdown.drained")
        except TimeoutError:
            _log.warning(
                "shutdown.drain_timeout",
                extra={"remaining": self._active_requests, "timeout": self.timeout},
            )

    async def _wait_for_idle(self) -> None:
        """Wait until active requests drain. Called within a timeout by drain()."""
        while self._active_requests > 0:
            self._idle_event.clear()
            await self._idle_event.wait()


class ShutdownMiddleware:
    """ASGI middleware that tracks active requests for graceful shutdown.

    Attach this as the outermost middleware so it wraps all requests,
    including those that error before reaching route handlers.

    Usage::

        manager = ShutdownManager()
        app.add_middleware(ShutdownMiddleware, manager=manager)  # noqa
    """

    def __init__(self, app: Any, manager: ShutdownManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        self.manager.request_started()
        try:
            await self.app(scope, receive, send)
        finally:
            self.manager.request_finished()
