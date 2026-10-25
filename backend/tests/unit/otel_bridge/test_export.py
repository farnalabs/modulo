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
from tests.unit.otel_bridge.conftest import reset_global_provider, span_processors


def test_setup_otel_installs_a_provider_carrying_the_service_name() -> None:
    """The global provider is replaced and stamped with the requested service name.

    Also covers "the default is telemetry off": with ``telemetry_enabled`` left at
    its default the provider must be installed but register no exporters.
    """
    setup_otel(service_name="my-modulo")

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes.get("service.name") == "my-modulo"
    assert not span_processors(provider), "no exporter may be registered when telemetry is off"

    # The provider must be usable: spans can be created against it.
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("test", True)
    assert span.name == "test-span"


def test_setup_otel_uses_default_service_name() -> None:
    setup_otel()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes.get("service.name") == "modulo"


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
    assert not span_processors(provider)


def test_setup_otel_enabled_registers_stdout_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry on with no endpoint set: exactly the stdout SimpleSpanProcessor.

    ``TestTelemetryEnabled.test_enabled_registers_stdout_exporter`` pins the
    enabled/disabled toggle matrix; this pins *which* processor the exporter
    lands in (Simple, not Batch).
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup_otel(service_name="enabled", telemetry_enabled=True)
    processors = span_processors(trace.get_tracer_provider())
    assert len(processors) == 1
    assert isinstance(processors[0], SimpleSpanProcessor)


def test_setup_otel_with_otlp_endpoint_registers_batch_processor_after_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set OTLP endpoint adds a Batch processor alongside the stdout one.

    The exporter constructor is patched so nothing is exported over the network —
    only the processor wiring is under test here.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://nonexistent.local:4318/v1/traces")
    with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_exporter:
        setup_otel(service_name="with-otlp", telemetry_enabled=True)
    mock_exporter.assert_called_once()
    processors = span_processors(trace.get_tracer_provider())
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
    # Stdout processor must still be registered, and no half-built Batch processor.
    processors = span_processors(provider)
    assert len(processors) == 1
    assert isinstance(processors[0], SimpleSpanProcessor)


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


def test_shutdown_otel_with_provider_without_shutdown_method() -> None:
    """A ProxyTracerProvider (no shutdown()) is ignored and left installed."""
    proxy_provider = MagicMock(spec=[])
    trace.set_tracer_provider(proxy_provider)
    shutdown_otel()  # should not raise
    # The caller's provider must be left untouched — not swapped or shut down.
    assert trace.get_tracer_provider() is proxy_provider


# ---------------------------------------------------------------------------
# _sanitise_url — credential redaction for log lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected", "secrets"),
    [
        pytest.param(
            "https://otel.example.com:4318/v1/traces",
            "https://otel.example.com:4318/v1/traces",
            (),
            id="no-credentials-with-port",
        ),
        pytest.param(
            "https://user:pass@otel.example.com:4318/v1/traces",
            "https://otel.example.com:4318/v1/traces",
            ("user", "pass"),
            id="username-and-password-with-port",
        ),
        pytest.param(
            "https://user@otel.example.com:4318/v1/traces",
            "https://otel.example.com:4318/v1/traces",
            ("user",),
            id="username-only-with-port",
        ),
        pytest.param(
            "https://user:pass@otel.example.com/v1/traces",
            "https://otel.example.com/v1/traces",
            ("user", "pass"),
            id="username-and-password-no-port",
        ),
        pytest.param(
            "https://:secret@otel.example.com/v1/traces",
            "https://otel.example.com/v1/traces",
            ("secret",),
            id="password-only-no-port",
        ),
        pytest.param(
            "https://user:pass@otel.example.com:8443/v1/traces",
            "https://otel.example.com:8443/v1/traces",
            ("user", "pass"),
            id="non-default-port-preserved",
        ),
        pytest.param(
            "https://@otel.example.com/v1/traces",
            "https://@otel.example.com/v1/traces",
            (),
            id="empty-userinfo-kept",
        ),
    ],
)
def test_sanitise_url_strips_credentials_but_preserves_host_and_port(
    url: str, expected: str, secrets: tuple[str, ...]
) -> None:
    """Credentials must be redacted before a URL reaches a log line.

    The host and port must survive so an operator can still tell which collector
    was configured. ``id`` names the case so a failure is readable in CI output.
    """
    sanitised = _sanitise_url(url)
    assert sanitised == expected
    for secret in secrets:
        assert secret not in sanitised, f"credential {secret!r} leaked into {sanitised!r}"
