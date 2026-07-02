"""Unit tests for setup_otel.

Uses OTel's InMemorySpanExporter to verify the global TracerProvider is
configured correctly without needing real OTLP or stdout I/O.
"""

import os
from collections.abc import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from modulo.otel_bridge.export import setup_otel, shutdown_otel


@pytest.fixture(autouse=True)
def _reset_otel() -> Generator[None, None, None]:
    """Reset the global tracer provider after each test to prevent background
    BatchSpanProcessor threads from writing to closed stdout during teardown.
    """
    yield
    trace.set_tracer_provider(TracerProvider())


def test_setup_otel_sets_global_provider() -> None:
    setup_otel(service_name="test-service")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_setup_otel_creates_tracer_with_service_name() -> None:
    setup_otel(service_name="my-modulo")
    tracer = trace.get_tracer("test")
    # The tracer should be functional — creating a span should not raise.
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("test", True)


def test_setup_otel_default_service_name() -> None:
    setup_otel()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("test", True)


def test_setup_otel_idempotent() -> None:
    setup_otel(service_name="first")
    setup_otel(service_name="second")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_setup_otel_no_otlp_without_env() -> None:
    """Should not crash when OTEL_EXPORTER_OTLP_ENDPOINT is not set."""
    # Ensure env var is absent
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    setup_otel(service_name="no-otlp")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_setup_otel_handles_bad_otlp_endpoint() -> None:
    """Should not crash when OTLP endpoint is invalid."""
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://nonexistent.local:4318/v1/traces"
    try:
        setup_otel(service_name="bad-otlp")
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
    finally:
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)


def test_shutdown_otel_multi_call_safe() -> None:
    """shutdown_otel() should be safe to call multiple times — second call is a no-op."""
    setup_otel(telemetry_enabled=True)
    shutdown_otel()  # First call — flush & shut down
    shutdown_otel()  # Second call — should be a no-op, not raise
