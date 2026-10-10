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


@pytest.fixture(scope="package", autouse=True)
def _cache_ssl_context_for_mocked_http():
    """Package-scoped cache of the httpx TLS context (test-only speedup).

    Every connector builds a fresh ``httpx.AsyncClient`` per request via
    ``pinned_async_client_sync``; httpx then parses the certifi CA bundle
    (~66 ms) to build the client's TLS context. All HTTP in this package is
    mocked (respx), so the context is never used for a real handshake.

    Only the ``trust_env=False`` path is cached — the ``trust_env=True`` path
    reads ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``, so caching it would be
    env-blind. The patch is package-scoped (finalised when this package's last
    test completes), so it cannot leak into other test packages, and the
    original is restored in a ``finally``.

    NOTE: building a fresh client per request is a real production
    inefficiency (``rest`` already reuses one client; the other connectors do
    not). Fixing it is a ``backend/src/`` change, out of scope for this
    test-only pass, and is reported as an outstanding item.
    """
    import functools

    import httpx._transports.default as _httpx_default

    real = getattr(_httpx_default, "create_ssl_context", None)
    if real is None or getattr(real, "__wrapped__", None) is not None:
        yield
        return

    @functools.lru_cache(maxsize=16)
    def _cached(verify, cert):
        return real(verify=verify, cert=cert, trust_env=False)

    def _create_ssl_context(verify=True, cert=None, trust_env=True):
        if trust_env:
            # Env-dependent (SSL_CERT_FILE / SSL_CERT_DIR) — never cache.
            return real(verify=verify, cert=cert, trust_env=trust_env)
        try:
            return _cached(verify, cert)
        except TypeError:  # unhashable cert — fall back to an uncached call
            return real(verify=verify, cert=cert, trust_env=False)

    _httpx_default.create_ssl_context = _create_ssl_context
    try:
        yield
    finally:
        _httpx_default.create_ssl_context = real


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
