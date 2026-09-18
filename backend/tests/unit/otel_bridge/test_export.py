"""Unit tests for setup_otel.

Uses OTel's InMemorySpanExporter to verify the global TracerProvider is
configured correctly without needing real OTLP or stdout I/O.
"""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from modulo.otel_bridge.export import _sanitise_url, setup_otel, shutdown_otel
from tests.unit.otel_bridge.conftest import reset_global_provider


def test_setup_otel_sets_global_provider() -> None:
    setup_otel(service_name="test-service")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_setup_otel_sets_service_name_resource() -> None:
    setup_otel(service_name="my-modulo")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes.get("service.name") == "my-modulo"


def test_setup_otel_uses_default_service_name() -> None:
    setup_otel()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes.get("service.name") == "modulo"


def test_setup_otel_creates_tracer_with_service_name() -> None:
    setup_otel(service_name="my-modulo")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # The requested service name must be reflected in the provider resource.
    assert provider.resource.attributes.get("service.name") == "my-modulo"
    # The tracer should be functional — creating a span should not raise.
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("test", True)


def test_setup_otel_can_be_called_repeatedly(caplog: pytest.LogCaptureFixture) -> None:
    """Repeated calls are safe — no crash and a usable provider remains.

    OTel only permits one global TracerProvider per process, so a second call
    does not replace the first; the documented contract is that repeated calls
    are harmless rather than that the provider is swapped out. The second call
    logs OTel's expected override warning, which is asserted here so the
    documented semantics are pinned rather than incidental noise.
    """
    setup_otel(service_name="first")
    with caplog.at_level(logging.WARNING, logger="opentelemetry.trace"):
        setup_otel(service_name="second")
    assert any("Overriding of current TracerProvider" in r.message for r in caplog.records)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # The provider must remain usable for span creation after the second call.
    tracer = trace.get_tracer("repeat")
    with tracer.start_as_current_span("repeat-span") as span:
        span.set_attribute("repeat", True)
    assert span.name == "repeat-span"


def test_setup_otel_disabled_registers_no_span_processors() -> None:
    setup_otel(service_name="disabled")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert not provider._active_span_processor._span_processors


def test_setup_otel_enabled_registers_stdout_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup_otel(service_name="enabled", telemetry_enabled=True)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    processors = provider._active_span_processor._span_processors
    assert len(processors) == 1
    assert isinstance(processors[0], SimpleSpanProcessor)


def test_setup_otel_no_otlp_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should not configure OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is not set."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_exporter:
        setup_otel(service_name="no-otlp", telemetry_enabled=True)
    mock_exporter.assert_not_called()


def test_setup_otel_handles_bad_otlp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should not crash when OTLP endpoint is invalid; stdout still configured."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://nonexistent.local:4318/v1/traces")
    with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_exporter:
        setup_otel(service_name="bad-otlp", telemetry_enabled=True)
    mock_exporter.assert_called_once()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # The OTLP path still registers its BatchSpanProcessor alongside the
    # stdout processor — the exporter is constructed (lazily, no network).
    processors = provider._active_span_processor._span_processors
    assert len(processors) == 2
    assert isinstance(processors[0], SimpleSpanProcessor)
    assert isinstance(processors[1], BatchSpanProcessor)


def test_setup_otel_otlp_constructor_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing OTLPSpanExporter constructor must not break setup_otel."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otlp.invalid:4318/v1/traces")
    with patch("modulo.otel_bridge.export.OTLPSpanExporter", side_effect=RuntimeError("boom")):
        setup_otel(service_name="otlp-fails", telemetry_enabled=True)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # Stdout processor must still be registered.
    assert len(provider._active_span_processor._span_processors) == 1


def test_shutdown_otel_multi_call_safe() -> None:
    """shutdown_otel() should be safe to call multiple times — second call is a no-op."""
    setup_otel(telemetry_enabled=True)
    shutdown_otel()  # First call — flush & shut down
    shutdown_otel()  # Second call — should be a no-op, not raise
    # The provider must still be a real TracerProvider after shutdown.
    assert isinstance(trace.get_tracer_provider(), TracerProvider)


def test_shutdown_otel_handles_provider_shutdown_failure(caplog) -> None:
    """A provider whose shutdown() raises must not propagate the exception."""
    bad_provider = MagicMock()
    bad_provider.shutdown.side_effect = RuntimeError("shutdown failed")
    trace.set_tracer_provider(bad_provider)
    with caplog.at_level(logging.ERROR):
        shutdown_otel()  # should log and swallow the error
    # The failure path must be exercised and surfaced, not silently skipped.
    assert any("Failed to shut down OTel provider" in r.message for r in caplog.records)


def test_setup_otel_otlp_constructor_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled OTLP exporter constructor must not be swallowed."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otlp.invalid:4318/v1/traces")
    with (
        patch("modulo.otel_bridge.export.OTLPSpanExporter", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        setup_otel(service_name="otlp-cancelled", telemetry_enabled=True)


def test_shutdown_otel_provider_cancellation_propagates() -> None:
    """A cancelled provider shutdown() must propagate CancelledError."""
    cancelled_provider = MagicMock()
    cancelled_provider.shutdown.side_effect = asyncio.CancelledError
    trace.set_tracer_provider(cancelled_provider)
    with pytest.raises(asyncio.CancelledError):
        shutdown_otel()
    # Restore a clean provider so the autouse teardown's shutdown_otel() no-ops.
    reset_global_provider()


def test_shutdown_otel_with_plain_proxy_provider() -> None:
    """Providers without a shutdown() method are ignored gracefully."""
    trace.set_tracer_provider(MagicMock(spec=[]))
    shutdown_otel()  # should not raise
    # The caller's provider must be left untouched.
    assert trace.get_tracer_provider() is not None


# ---------------------------------------------------------------------------
# _sanitise_url — credential redaction for log lines
# ---------------------------------------------------------------------------


def test_sanitise_url_leaves_credential_free_url_unchanged() -> None:
    url = "https://otel.example.com:4318/v1/traces"
    assert _sanitise_url(url) == url


def test_sanitise_url_strips_username_and_password() -> None:
    url = "https://user:pass@otel.example.com:4318/v1/traces"
    sanitised = _sanitise_url(url)
    assert "user" not in sanitised
    assert "pass" not in sanitised
    assert sanitised == "https://otel.example.com:4318/v1/traces"


def test_sanitise_url_strips_username_only() -> None:
    url = "https://user@otel.example.com:4318/v1/traces"
    assert _sanitise_url(url) == "https://otel.example.com:4318/v1/traces"


def test_sanitise_url_handles_no_port() -> None:
    url = "https://user:pass@otel.example.com/v1/traces"
    assert _sanitise_url(url) == "https://otel.example.com/v1/traces"


def test_sanitise_url_username_only_with_port() -> None:
    url = "https://user@otel.example.com:8443/v1/traces"
    assert _sanitise_url(url) == "https://otel.example.com:8443/v1/traces"


def test_sanitise_url_password_only_without_username() -> None:
    """A password with an empty username is still stripped."""
    url = "https://:secret@otel.example.com/v1/traces"
    assert _sanitise_url(url) == "https://otel.example.com/v1/traces"


def test_sanitise_url_empty_credentials_kept() -> None:
    """An empty userinfo (bare '@') carries no credentials to strip."""
    url = "https://@otel.example.com/v1/traces"
    assert _sanitise_url(url) == "https://@otel.example.com/v1/traces"
