"""OTel exporter configuration.

Configures the global TracerProvider with exporters based on environment:
- Stdout (ConsoleSpanExporter) — disabled by default, requires MODULO_TELEMETRY_ENABLED=true
- OTLP — requires both MODULO_TELEMETRY_ENABLED=true and OTEL_EXPORTER_OTLP_ENDPOINT set
- LangSmith — configured externally via LANGCHAIN_TRACING_V2 + LANGCHAIN_API_KEY env vars

Sensitive data (credentials, API keys, user content) is never written to span attributes.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

_log = logging.getLogger(__name__)


def setup_otel(
    service_name: str = "modulo",
    telemetry_enabled: bool = False,
) -> None:
    """Configure the global OTel TracerProvider.

    When **telemetry_enabled** is False (the default), no exporters are
    registered — no telemetry data leaves the process. This satisfies
    the data residency requirement that telemetry is opt-in only.

    When enabled, the stdout exporter is always active for local debugging.
    The OTLP exporter is additionally activated when the
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` environment variable is set.

    Call once at application startup before any spans are created. Idempotent —
    replaces the global provider if already set.
    """
    if not telemetry_enabled:
        _log.info(
            "OTel telemetry is DISABLED (MODULO_TELEMETRY_ENABLED not set to true). No telemetry data will be exported."
        )
        # Create a no-op provider that drops all spans
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(provider)
        return

    # Shut down any previously configured provider before replacing
    shutdown_otel()

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # Stdout exporter — always enabled when telemetry is on.
    # Prints spans as JSON lines for local debugging. Uses SimpleSpanProcessor
    # (not BatchSpanProcessor) since the console exporter is synchronous.
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    _log.info("OTel stdout exporter configured")

    # OTLP exporter — enabled when the endpoint env var is set.
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        try:
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            _log.info("OTel OTLP exporter configured: endpoint=%s", otlp_endpoint)
        except Exception:
            _log.exception("Failed to configure OTLP exporter; continuing without it")

    trace.set_tracer_provider(provider)


def shutdown_otel() -> None:
    """Flush and shut down the global TracerProvider.

    Call during application shutdown to ensure all buffered spans are exported
    before the process exits. Safe to call multiple times — subsequent calls
    are no-ops once the provider is shut down.
    """
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:
            _log.exception("Failed to shut down OTel provider")
