"""Shared fixtures and helpers for otel_bridge unit tests."""

from collections.abc import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

from modulo.otel_bridge.export import shutdown_otel


def reset_global_provider() -> None:
    """Replace the global TracerProvider.

    OTel allows set_tracer_provider() to be called only once per process, so
    the internal Once guard must be reset before swapping the provider. The
    provider is assigned directly (not via set_tracer_provider) so the fresh
    guard stays unconsumed, letting each test's setup_otel() call take effect.
    """
    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = TracerProvider()  # type: ignore[attr-defined]


def span_processors(provider: TracerProvider) -> tuple:
    return provider._active_span_processor._span_processors  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def reset_otel() -> Generator[None, None, None]:
    """Reset the global TracerProvider before and after each test.

    Resetting before each test keeps the provider isolated between tests, and
    shutting down in teardown flushes and stops any OTLP BatchSpanProcessor
    background thread before the provider is replaced.
    """
    reset_global_provider()
    yield
    shutdown_otel()
    reset_global_provider()
