"""FAR-1128 cap-observability: real OTLP export to a loopback collector.

Stands up a real OTLP/HTTP receiver on 127.0.0.1 (``http.server`` +
``trace_service_pb2`` protobuf decoding) and points ``setup_otel`` at it via
``OTEL_EXPORTER_OTLP_ENDPOINT``. Because ``OTLPSpanExporter(endpoint=...)``
posts to the configured URL verbatim, the endpoint includes the trailing
``/v1/traces`` path and the receiver asserts it. Proves end-to-end that spans
leave the process as real protobuf over HTTP when telemetry is enabled, and
that disabling telemetry sends nothing.
"""

from __future__ import annotations

import gzip
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

from modulo.otel_bridge.export import setup_otel, shutdown_otel


def _reset_global_provider() -> None:
    """Replace the OTel global TracerProvider, clearing the once-guard.

    Mirrors ``tests/unit/otel_bridge/conftest.py`` so each test can install a
    fresh provider. Assigning directly (not via ``set_tracer_provider``) keeps
    the fresh ``Once`` guard unconsumed for the next ``setup_otel`` call.
    """
    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = TracerProvider()  # type: ignore[attr-defined]


class _OtlpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
        self.server.received.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        return


class _OtlpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _OtlpHandler)
        self.received: list[tuple[str, bytes]] = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1/traces"


@pytest.fixture
def otlp_server() -> Generator[_OtlpServer, None, None]:
    server = _OtlpServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_otel_provider() -> Generator[None, None, None]:
    _reset_global_provider()
    yield
    shutdown_otel()
    _reset_global_provider()


def _wait_for_receiver(server: _OtlpServer, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not server.received:
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


class TestCapObservabilityRealExport:
    def test_enabled_telemetry_posts_protobuf_path_and_span(
        self,
        otlp_server: _OtlpServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_server.endpoint)
        setup_otel(service_name="modulo-cap-observability", telemetry_enabled=True)

        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

        tracer = provider.get_tracer("cap-observability-tests")
        span = tracer.start_span("license.verify")
        span.set_attribute("modulo.tier", "team")
        span.end()
        provider.force_flush(timeout_millis=5000)

        _wait_for_receiver(otlp_server)
        assert otlp_server.received

        paths = {path for path, _body in otlp_server.received}
        assert "/v1/traces" in paths

        request = trace_service_pb2.ExportTraceServiceRequest.FromString(otlp_server.received[0][1])
        span_names = [s.name for rss in request.resource_spans for scope in rss.scope_spans for s in scope.spans]
        assert "license.verify" in span_names

        attributes = {
            attr.key: attr.value.string_value
            for rss in request.resource_spans
            for scope in rss.scope_spans
            for span in scope.spans
            for attr in span.attributes
        }
        assert attributes.get("modulo.tier") == "team"

    def test_disabled_telemetry_sends_nothing(self, otlp_server: _OtlpServer, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_server.endpoint)
        setup_otel(service_name="modulo-cap-observability", telemetry_enabled=False)

        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

        tracer = provider.get_tracer("cap-observability-tests")
        span = tracer.start_span("license.verify")
        span.set_attribute("modulo.tier", "team")
        span.end()
        provider.force_flush(timeout_millis=2000)

        assert not otlp_server.received
