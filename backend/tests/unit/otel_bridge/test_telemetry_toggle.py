"""Unit tests for telemetry enable/disable toggle.

Verifies that MODULO_TELEMETRY_ENABLED controls whether OTel
exporters are registered, enabling data residency compliance.
"""

from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from modulo.otel_bridge.export import setup_otel
from tests.unit.otel_bridge.conftest import span_processors


class TestTelemetryDefaults:
    """Tests that telemetry is disabled by default."""

    def test_telemetry_disabled_registers_no_exporters(self):
        """telemetry_enabled=False should register no span processors."""
        setup_otel(telemetry_enabled=False)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert not span_processors(provider)

    def test_disabled_stdout_exporter_not_used(self):
        """When disabled, the ConsoleSpanExporter must not be instantiated."""
        with patch("modulo.otel_bridge.export.ConsoleSpanExporter") as mock_console:
            setup_otel(telemetry_enabled=False)
        mock_console.assert_not_called()

    def test_disabled_otlp_exporter_not_used(self, monkeypatch: pytest.MonkeyPatch):
        """When disabled, OTLP must not be configured even with endpoint set."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_otlp:
            setup_otel(telemetry_enabled=False)
        mock_otlp.assert_not_called()

    def test_disabled_tracer_still_works(self):
        """Span creation must work with telemetry disabled."""
        setup_otel(telemetry_enabled=False)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test") as span:
            span.set_attribute("test", True)
        assert span.name == "test"


class TestTelemetryEnabled:
    """Tests that telemetry is properly configured when enabled."""

    def test_enabled_registers_stdout_exporter(self, monkeypatch: pytest.MonkeyPatch):
        """telemetry_enabled=True should register a stdout span processor."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert len(span_processors(provider)) == 1

    def test_enabled_instantiates_console_exporter(self):
        """When enabled, the ConsoleSpanExporter should be constructed."""
        with patch("modulo.otel_bridge.export.ConsoleSpanExporter") as mock_console:
            setup_otel(telemetry_enabled=True)
        mock_console.assert_called_once()

    def test_tracer_works_when_enabled(self):
        """Creating spans should work when telemetry is enabled."""
        setup_otel(telemetry_enabled=True)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test-enabled") as span:
            span.set_attribute("key", "value")
        assert span.name == "test-enabled"

    def test_otlp_not_configured_without_env(self, monkeypatch: pytest.MonkeyPatch):
        """OTLP exporter should not be configured when env var is absent."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_otlp:
            setup_otel(telemetry_enabled=True)
        mock_otlp.assert_not_called()

    def test_otlp_configured_with_env(self, monkeypatch: pytest.MonkeyPatch):
        """OTLP exporter should be constructed when endpoint env var is set."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        with patch("modulo.otel_bridge.export.OTLPSpanExporter") as mock_otlp:
            setup_otel(telemetry_enabled=True)
        mock_otlp.assert_called_once()

    def test_otlp_configured_with_env_keeps_stdout(self, monkeypatch: pytest.MonkeyPatch):
        """Both stdout and OTLP processors should be active when endpoint is set."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        setup_otel(telemetry_enabled=True)
        provider = trace.get_tracer_provider()
        assert len(span_processors(provider)) == 2


class TestSettingsIntegration:
    """Tests the Settings model integration with telemetry."""

    def _settings(self, **overrides):
        from modulo.settings import Settings

        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
            **overrides,
        )

    def test_settings_defaults_to_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """Settings.modulo_telemetry_enabled should default to False."""
        # Isolate from any ambient MODULO_TELEMETRY_ENABLED in the runner env.
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)
        assert self._settings().modulo_telemetry_enabled is False

    def test_settings_can_enable(self, monkeypatch: pytest.MonkeyPatch):
        """Settings.modulo_telemetry_enabled can be set to True."""
        monkeypatch.delenv("MODULO_TELEMETRY_ENABLED", raising=False)
        assert self._settings(modulo_telemetry_enabled=True).modulo_telemetry_enabled is True

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch):
        """MODULO_TELEMETRY_ENABLED env var should override the default."""
        monkeypatch.setenv("MODULO_TELEMETRY_ENABLED", "true")
        assert self._settings().modulo_telemetry_enabled is True
