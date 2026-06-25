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


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter: InMemorySpanExporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test")


@pytest.fixture()
def inner():
    return _FakeConnector()


@pytest.fixture()
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


async def test_health_check_creates_span(
    traced: _TracedConnector, exporter: InMemorySpanExporter
) -> None:
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


async def test_traced_connector_without_org_id(
    traced: _TracedConnector, exporter: InMemorySpanExporter
) -> None:
    await traced.health_check()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    if spans[0].attributes:
        assert "connector.org_id" not in spans[0].attributes


async def test_query_error_records_exception(
    traced: _TracedConnector, exporter: InMemorySpanExporter
) -> None:
    inner = traced._inner

    with patch.object(inner, "query", AsyncMock(side_effect=ValueError("connection failed"))):
        with pytest.raises(ValueError, match="connection failed"):
            await traced.query(ConnectorQuery(resource="/test"))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    event_names = [e.name for e in span.events]
    assert "exception" in event_names


async def test_write_error_records_exception(
    traced: _TracedConnector, exporter: InMemorySpanExporter
) -> None:
    inner = traced._inner

    with patch.object(inner, "write", AsyncMock(side_effect=PermissionError("access denied"))):
        with pytest.raises(PermissionError, match="access denied"):
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
def _hub_global_exporter() -> InMemorySpanExporter:
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


async def test_hub_integration_health_check(
    tmp_path, _hub_global_exporter: InMemorySpanExporter
) -> None:
    """ConnectorHub wiring produces spans in health_check."""
    key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt_with(key, {}),
    )

    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, 'get_secret', return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend, org_id="org-42")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)
            result = await connector.health_check()
            assert result.ok is True

    spans = _hub_global_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes is not None
    assert span.attributes.get("connector.type") == "filesystem"
    assert span.attributes.get("connector.operation") == "health_check"
    assert span.attributes.get("connector.org_id") == "org-42"
    assert span.attributes.get("connector.healthy") is True


async def test_hub_integration_query_and_write(
    tmp_path, _hub_global_exporter: InMemorySpanExporter
) -> None:
    """org_id flows through hub to query and write spans."""
    _hub_global_exporter.clear()

    key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt_with(key, {}),
    )

    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, 'get_secret', return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend, org_id="tenant-abc")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)

            await connector.query(
                ConnectorQuery(resource="directory", filters={"path": str(tmp_path)})
            )
            out_path = tmp_path / "out.txt"
            await connector.write(
                ConnectorPayload(resource="file", data={"content": "hello", "path": str(out_path)})
            )

    spans = _hub_global_exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes is not None
        assert span.attributes.get("connector.org_id") == "tenant-abc"
