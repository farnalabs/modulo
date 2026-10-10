"""Shared fixtures for the connector-hub unit tests.

Pins ``modulo.settings.get_settings`` to a settings read with the shared
Redis budget unconfigured. The hub's tenant path (``org_id`` set) is
settings-reading AND fail-closed (FAR-439): without this pin a bare tenant
fixture would raise ``SharedBudgetUnavailableError`` in any environment
where Settings() cannot build from env vars. Tests that specifically patch
``get_settings`` themselves re-patch and win as usual.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _pin_settings_for_tenant_paths() -> None:
    # MagicMock default is truthy for every attribute; explicitly make the
    # shared Redis budget look unconfigured so the tenant path resolves None.
    settings = MagicMock()
    settings.redis_url = None
    patcher = patch("modulo.settings.get_settings", return_value=settings)
    patcher.start()
    yield
    patcher.stop()


@pytest.fixture(scope="session", autouse=True)
def _cache_ssl_contexts_for_mocked_http():
    """Session-cache the httpx/httpcore TLS context.

    Every connector builds a fresh httpx.AsyncClient per request via
    pinned_async_client_sync; httpcore then parses the certifi CA bundle
    (~66 ms) for each. All HTTP in this package is mocked (respx), so the
    context is never used for a real handshake — caching it removes a large
    fraction of package wall time. Restored on teardown.
    """
    import functools

    import httpcore._ssl as _httpcore_ssl
    import httpx._transports.default as _httpx_default

    originals = []
    for mod, name in (
        (_httpcore_ssl, "default_ssl_context"),
        (_httpx_default, "create_ssl_context"),
    ):
        fn = getattr(mod, name)
        if not getattr(fn, "__wrapped__", None):
            originals.append((mod, name, fn))
            setattr(mod, name, functools.lru_cache(maxsize=None)(fn))
    yield
    for mod, name, fn in originals:
        setattr(mod, name, fn)


@pytest.fixture
def exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test")


@pytest.fixture(scope="session")
def otel_span_exporter():
    """Single session-scoped in-memory span exporter bound to the global provider.

    Shared by every hub-integration test in this package so the module-scoped
    provider/exporter churn (test_connector_hub_e2e.py + test_traced_connector.py
    each calling setup_otel and attaching their own exporter) can no longer make
    a test depend on which module ran first. Callers MUST clear it at the start
    of the test.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from modulo.otel_bridge.export import setup_otel

    setup_otel(service_name="connector-hub-tests")
    span_exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return span_exporter
