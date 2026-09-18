"""Unit tests for _TracedConnector OTel span wrapping.

Uses OTel's InMemorySpanExporter — no network, no DB.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.core.connector_hub import ConnectorHub, _TracedConnector
from modulo.core.secrets_backend import create_secrets_backend


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test")


@pytest.fixture
def inner():
    return _FakeConnector()


@pytest.fixture
def traced(inner, tracer):
    return _TracedConnector(inner, tracer=tracer)


@dataclass
class _FakeConnector(ConnectorBase):
    """Minimal connector that returns canned results."""

    _connector_type: ConnectorType = ConnectorType.FILESYSTEM

    @property
    def connector_type(self) -> ConnectorType:
        return self._connector_type

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True, detail="healthy")

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[{"file": "test.txt"}], total=1)

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        return {"status": "ok", "path": payload.resource}


async def test_health_check_creates_span(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    result = await traced.health_check()

    assert result.ok is True
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert "filesystem" in span.name
    assert span.attributes is not None
    assert span.attributes.get("connector.type") == "filesystem"
    assert span.attributes.get("connector.operation") == "health_check"
    assert span.attributes.get("connector.healthy") is True
    assert span.status.status_code == StatusCode.OK


async def test_query_creates_span(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    q = ConnectorQuery(resource="/test", filters={"ext": ".txt"}, limit=10)
    result = await traced.query(q)

    assert len(result.records) == 1
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert "filesystem" in span.name
    assert span.attributes is not None
    assert span.attributes.get("connector.type") == "filesystem"
    assert span.attributes.get("connector.operation") == "query"
    assert span.attributes.get("connector.resource") == "/test"
    assert span.attributes.get("connector.limit") == 10
    assert span.attributes.get("connector.result_total") == 1

    # Sensitive data NEVER in span attributes
    assert "connector.filter" not in span.attributes
    assert span.attributes.get("connector.query") is None


async def test_write_creates_span(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    payload = ConnectorPayload(resource="/test/output.txt", data={"content": "secret data"})
    result = await traced.write(payload)

    assert result["status"] == "ok"
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert "filesystem" in span.name
    assert span.attributes is not None
    assert span.attributes.get("connector.type") == "filesystem"
    assert span.attributes.get("connector.operation") == "write"
    assert span.attributes.get("connector.resource") == "/test/output.txt"

    # Sensitive data NEVER in span attributes
    assert "connector.data" not in span.attributes
    assert span.attributes.get("connector.content") is None


async def test_traced_connector_with_org_id(tracer, exporter: InMemorySpanExporter) -> None:
    inner = _FakeConnector()
    traced = _TracedConnector(inner, tracer=tracer, org_id="org-123")

    await traced.health_check()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes is not None
    assert spans[0].attributes.get("connector.org_id") == "org-123"


async def test_traced_connector_without_org_id(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    await traced.health_check()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    if spans[0].attributes:
        assert "connector.org_id" not in spans[0].attributes


async def test_query_error_records_exception(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    inner = traced._inner

    with (
        patch.object(inner, "query", AsyncMock(side_effect=ValueError("connection failed"))),
        pytest.raises(ValueError, match="connection failed"),
    ):
        await traced.query(ConnectorQuery(resource="/test"))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    event_names = [e.name for e in span.events]
    assert "exception" in event_names
    assert span.attributes is not None
    assert span.attributes.get("connector.error_type") == "ValueError"


async def test_write_error_records_exception(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    inner = traced._inner

    with (
        patch.object(inner, "write", AsyncMock(side_effect=PermissionError("access denied"))),
        pytest.raises(PermissionError, match="access denied"),
    ):
        await traced.write(ConnectorPayload(resource="/test", data={}))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    event_names = [e.name for e in span.events]
    assert "exception" in event_names


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=bytes)
    visibility: str = "org"
    allowed_operations: list[str] | None = None


def _encrypt_with(key: str, d: dict[str, Any]) -> bytes:
    return Fernet(key.encode()).encrypt(json.dumps(d).encode())


@pytest.fixture(scope="module")
def hub_global_exporter() -> InMemorySpanExporter:
    """Module-scoped InMemorySpanExporter for ConnectorHub integration tests.

    Calls setup_otel to ensure a fresh TracerProvider, then adds an
    InMemorySpanExporter processor to capture spans in-memory.
    """
    from modulo.otel_bridge.export import setup_otel

    setup_otel(service_name="test-hub")
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def test_traced_connector_getattr_proxies_to_inner(traced: _TracedConnector) -> None:
    """Unknown attributes are proxied to the inner connector."""
    inner = traced._inner
    inner.custom_method = lambda: "proxied"  # type: ignore[attr-defined]

    assert traced.custom_method() == "proxied"  # type: ignore[attr-defined]


async def test_query_cancelled_records_error(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    """CancelledError is re-raised and the span is marked ERROR with a 'cancelled' status."""
    import asyncio

    inner = traced._inner
    with (
        patch.object(inner, "query", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await traced.query(ConnectorQuery(resource="/test"))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is not None
    assert "cancelled" in span.status.description


async def test_post_span_callback_failure_does_not_break_result(
    traced: _TracedConnector, exporter: InMemorySpanExporter, caplog
) -> None:
    """A failing post_span callback is logged but does not change the returned result."""
    import logging

    inner = traced._inner
    # Return a result without an `.ok` attribute so the health_check post_span callback fails.
    bad_result = object()
    with (
        patch.object(inner, "health_check", AsyncMock(return_value=bad_result)),
        caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub"),
    ):
        result = await traced.health_check()

    assert result is bad_result
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert any("post_span callback failed" in rec.message for rec in caplog.records)


async def test_write_deepcopies_payload(traced: _TracedConnector) -> None:
    """write() deep-copies the payload before passing it to the inner connector."""
    inner = traced._inner
    captured: dict[str, Any] = {}

    async def _fake_write(payload: ConnectorPayload) -> dict[str, Any]:
        captured["payload"] = payload
        return {"status": "ok"}

    with patch.object(inner, "write", AsyncMock(side_effect=_fake_write)):
        payload = ConnectorPayload(resource="/out.txt", data={"nested": {"k": "v"}})
        await traced.write(payload)

    assert captured["payload"] is not payload
    assert captured["payload"].data == {"nested": {"k": "v"}}


async def test_query_span_sets_result_total_only_when_not_none(tracer, exporter: InMemorySpanExporter) -> None:
    """post_span for query handles results whose total is None without error."""
    inner = _FakeConnector()

    async def _no_total(q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[{"file": "x.txt"}], total=None)

    with patch.object(inner, "query", AsyncMock(side_effect=_no_total)):
        traced = _TracedConnector(inner, tracer=tracer)
        result = await traced.query(ConnectorQuery(resource="/test"))

    assert result.total is None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "connector.result_total" not in (spans[0].attributes or {})


async def test_run_with_tracing_without_acl_operation(traced: _TracedConnector, exporter: InMemorySpanExporter) -> None:
    """_run_with_tracing with acl_operation=None skips ACL enforcement (no-op branch)."""
    inner = traced._inner

    result = await traced._run_with_tracing(
        "connector.filesystem.manual",
        "manual",
        inner.health_check,
        acl_operation=None,
    )

    assert result.ok is True
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes is not None
    assert span.attributes.get("connector.operation") == "manual"
    assert span.status.status_code == StatusCode.OK


async def test_hub_integration_health_check(tmp_path, hub_global_exporter: InMemorySpanExporter) -> None:
    """ConnectorHub wiring produces spans in health_check."""
    key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt_with(key, {}),
    )

    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend, org_id="org-42")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)
            result = await connector.health_check()
            assert result.ok is True

    spans = hub_global_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes is not None
    assert span.attributes.get("connector.type") == "filesystem"
    assert span.attributes.get("connector.operation") == "health_check"
    assert span.attributes.get("connector.org_id") == "org-42"
    assert span.attributes.get("connector.healthy") is True


async def test_hub_integration_query_and_write(tmp_path, hub_global_exporter: InMemorySpanExporter) -> None:
    """org_id flows through hub to query and write spans."""
    hub_global_exporter.clear()

    key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt_with(key, {}),
    )

    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend, org_id="tenant-abc")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)

            await connector.query(ConnectorQuery(resource="directory", filters={"path": str(tmp_path)}))
            out_path = tmp_path / "out.txt"
            await connector.write(ConnectorPayload(resource="file", data={"content": "hello", "path": str(out_path)}))

    spans = hub_global_exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes is not None
        assert span.attributes.get("connector.org_id") == "tenant-abc"


async def test_hub_org_connector_rejected_for_team_scoped_invocation(tmp_path) -> None:
    """FAR-516: an org-only connector is fail-closed rejected for a team-scoped run.

    A ConnectorHub wired with ``request_visibility="team"`` must deny a
    ``visibility == "org"`` connector at the connector-invocation gate (both
    ``get(operation=...)`` and ``query``/``write``), while the same connector
    stays permitted for an org-scoped invocation.
    """
    from modulo.connectors.base import ConnectorPermissionError

    key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt_with(key, {}),
        visibility="org",
    )

    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        # Team-scoped request: get(operation=...) must reject the org-only connector.
        hub = ConnectorHub(secrets_backend=backend, org_id="org-42", request_visibility="team")
        async with hub:
            await hub.initialise([ci])
            with pytest.raises(ConnectorPermissionError, match="team-scoped"):
                hub.get(ci.id, operation="read")

        # The _TracedConnector invocation gate rejects on query too.
        hub = ConnectorHub(secrets_backend=backend, org_id="org-42", request_visibility="team")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)
            with pytest.raises(ConnectorPermissionError, match="team-scoped"):
                await connector.query(ConnectorQuery(resource="directory"))

        # Org-scoped request: the same org-only connector is permitted.
        hub = ConnectorHub(secrets_backend=backend, org_id="org-42", request_visibility="org")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)
            result = await connector.query(ConnectorQuery(resource="directory", filters={"path": str(tmp_path)}))
            assert isinstance(result, ConnectorResult)
            assert hub.get(ci.id, operation="read") is not None
