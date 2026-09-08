"""Circuit breaker pattern for protecting external service calls.

Provides a reusable circuit breaker that prevents cascade failures when
downstream services (model backends, connectors, etc.) are unhealthy.

States:
    CLOSED: Normal operation. Failures are counted.
    OPEN: Too many failures. Calls are rejected immediately.
    HALF_OPEN: After a timeout, one test call is allowed through.

Usage::

    from modulo.core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen

    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30)

    try:
        result = await breaker.call(some_async_function, arg1, arg2)
    except CircuitBreakerOpen:
        # Service is circuit-broken, handle degradation
        pass
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(enum.Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit breaker is open."""

    def __init__(self, name: str, remaining_seconds: float) -> None:
        self.name = name
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit breaker '{name}' is open. "
            f"Retry in {remaining_seconds:.1f}s."
        )


class CircuitBreaker:
    """Circuit breaker for protecting external service calls.

    Args:
        name: Identifier for this circuit breaker (used in logging).
        failure_threshold: Number of consecutive failures before opening.
        recovery_timeout: Seconds to wait before trying again (half-open).
        success_threshold: Consecutive successes in half-open to close again.
        on_state_change: Optional callback for state transitions.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 1,
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self._on_state_change = on_state_change

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit breaker state (may auto-transition from OPEN to HALF_OPEN)."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self._set_state(CircuitState.HALF_OPEN)
        return self._state

    def _set_state(self, new_state: CircuitState) -> None:
        """Transition to a new state, firing the callback."""
        old_state = self._state
        if old_state == new_state:
            return
        self._state = new_state
        logger.info(
            "circuit_breaker.state_changed name=%s old=%s new=%s",
            self.name,
            old_state.value,
            new_state.value,
        )
        if self._on_state_change:
            try:
                self._on_state_change(self.name, old_state, new_state)
            except Exception:
                logger.exception("circuit_breaker.on_state_change_error name=%s", self.name)

    def _record_success(self) -> None:
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._failure_count = 0
                self._success_count = 0
                self._set_state(CircuitState.CLOSED)
        else:
            self._failure_count = 0
            self._success_count = 0

    def _record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._success_count = 0
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            self._set_state(CircuitState.OPEN)
        elif self._failure_count >= self.failure_threshold:
            self._set_state(CircuitState.OPEN)

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute a function through the circuit breaker.

        Args:
            func: Async callable to execute.
            *args: Positional arguments for the callable.
            **kwargs: Keyword arguments for the callable.

        Returns:
            The result of the callable.

        Raises:
            CircuitBreakerOpen: When the circuit is open.
            Any exception raised by the callable (when circuit is closed/half-open).
        """
        current_state = self.state  # May auto-transition OPEN -> HALF_OPEN

        if current_state == CircuitState.OPEN:
            remaining = self.recovery_timeout - (time.monotonic() - self._last_failure_time)
            raise CircuitBreakerOpen(self.name, max(0.0, remaining))

        try:
            result = await func(*args, **kwargs)
            async with self._lock:
                self._record_success()
            return result
        except Exception:
            async with self._lock:
                self._record_failure()
            raise

    def is_available(self) -> bool:
        """Check if the circuit breaker would allow a call right now."""
        return self.state != CircuitState.OPEN

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        self._failure_count = 0
        self._success_count = 0
        self._set_state(CircuitState.CLOSED)

    def get_stats(self) -> dict[str, Any]:
        """Return current circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


# Global circuit breaker registry for model backends and connectors
_model_backend_breakers: dict[str, CircuitBreaker] = {}
_connector_breakers: dict[str, CircuitBreaker] = {}


def get_model_backend_breaker(backend_id: str) -> CircuitBreaker:
    """Get or create a circuit breaker for a model backend."""
    if backend_id not in _model_backend_breakers:
        _model_backend_breakers[backend_id] = CircuitBreaker(
            name=f"model_backend:{backend_id}",
            failure_threshold=3,
            recovery_timeout=60.0,
        )
    return _model_backend_breakers[backend_id]


def get_connector_breaker(connector_id: str) -> CircuitBreaker:
    """Get or create a circuit breaker for a connector."""
    if connector_id not in _connector_breakers:
        _connector_breakers[connector_id] = CircuitBreaker(
            name=f"connector:{connector_id}",
            failure_threshold=3,
            recovery_timeout=60.0,
        )
    return _connector_breakers[connector_id]


def get_all_breakers() -> dict[str, CircuitBreaker]:
    """Return all registered circuit breakers."""
    all_breakers: dict[str, CircuitBreaker] = {}
    all_breakers.update(_model_backend_breakers)
    all_breakers.update(_connector_breakers)
    return all_breakers
