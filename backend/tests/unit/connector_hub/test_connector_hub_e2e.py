"""End-to-end integration test for the ConnectorHub lifecycle.

Walks the full documented lifecycle contract in one flow:

    ConnectorHub.initialise()  ->  connector method  ->  OTel span  ->  credential cleanup

Uses real Fernet decryption through the secrets backend, a real filesystem
connector for query/write, and an InMemorySpanExporter attached to the global
TracerProvider. No network and no database.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorQuery,
    ConnectorType,
)
from modulo.core.connector_hub import ConnectorHub, ConnectorNotFoundError
from modulo.core.secrets_backend import create_secrets_backend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=lambda: _encrypt({}))
    visibility: str = "org"
    allowed_operations: list[str] | None = None


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    """Module-scoped InMemorySpanExporter on the global provider.

    The hub binds its tracer in ``__init__``, so this fixture must run before
    each test constructs a hub — pytest guarantees that when it is requested.
    """
    from modulo.otel_bridge.export import setup_otel

    setup_otel(service_name="test-hub-e2e")
    span_exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return span_exporter


async def test_e2e_full_lifecycle_spans_and_credential_cleanup(tmp_path, exporter: InMemorySpanExporter):
    """initialise -> method -> span -> credential cleanup in a single flow."""
    exporter.clear()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt({}),
        visibility="team",
        allowed_operations=["read", "write"],
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend, org_id="org-e2e")
        async with hub:
            await hub.initialise([ci])
            assert hub.connector_ids == frozenset({ci.id})

            connector = hub.get(ci.id)
            assert connector.connector_type == ConnectorType.FILESYSTEM

            health = await connector.health_check()
            assert health.ok is True

            write_result = await connector.write(
                ConnectorPayload(resource="file", data={"path": "e2e.txt", "content": "e2e secret"})
            )
            assert write_result["bytes_written"] == len("e2e secret")

            read_result = await connector.query(ConnectorQuery(resource="file", filters={"path": "e2e.txt"}))
            assert read_result.records[0]["content"] == "e2e secret"

        # Credential cleanup contract: the hub no longer exposes the connector.
        assert not hub.connector_ids
        assert hub._initialised is False
        with pytest.raises(ConnectorNotFoundError):
            hub.get(ci.id)
        with pytest.raises(ConnectorNotFoundError):
            hub.acl(ci.id)

    spans = exporter.get_finished_spans()
    operations = sorted(span.attributes.get("connector.operation") for span in spans if span.attributes)
    assert operations == ["health_check", "query", "write"]
    for span in spans:
        attrs = span.attributes or {}
        assert attrs.get("connector.type") == "filesystem"
        assert attrs.get("connector.org_id") == "org-e2e"
        assert span.status.status_code == StatusCode.OK
        # No span attribute may carry credentials or payload content.
        assert "e2e secret" not in str(attrs)
        assert "connector.filter" not in attrs
        assert "connector.data" not in attrs
        assert "connector.content" not in attrs


async def test_e2e_credentials_decrypted_into_connector_and_close_releases(exporter: InMemorySpanExporter):
    """Real encrypted credentials decrypt through the backend into the connector,
    and close() releases every reference to the credential-bearing connector."""
    exporter.clear()
    token = "ghp_e2e_super_secret_token_123"
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        config_json={},
        credentials_ciphertext=_encrypt({"token": token}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend, org_id="org-e2e")

    with patch.object(backend, "get_secret", return_value=json.dumps({"token": token})):
        await hub.initialise([ci])

    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.GITHUB
    # The decrypted credential actually reached the connector.
    assert connector._inner._token == token

    # close() is the named shutdown lifecycle method.
    hub.close()
    assert not hub.connector_ids
    assert hub._initialised is False
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)

    # A closed hub can be re-initialised for the next run.
    with patch.object(backend, "get_secret", return_value=json.dumps({"token": token})):
        await hub.initialise([ci])
    assert hub.get(ci.id).connector_type == ConnectorType.GITHUB
    assert hub.get(ci.id)._inner._token == token


async def test_e2e_close_is_idempotent(exporter: InMemorySpanExporter):
    """close() is safe to call repeatedly and without an async context manager."""
    exporter.clear()
    ci = _FakeCI(id=uuid.uuid4(), connector_type_id="filesystem", config_json={"base_path": "/tmp"})
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
        assert hub.connector_ids == frozenset({ci.id})

        hub.close()
        hub.close()  # double-close is a no-op
        assert not hub.connector_ids


async def test_e2e_error_span_recorded(tmp_path, exporter: InMemorySpanExporter):
    """A connector error is recorded on an ERROR span with the error type."""
    exporter.clear()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend, org_id="org-e2e")
        async with hub:
            await hub.initialise([ci])
            connector = hub.get(ci.id)
            with pytest.raises(ValueError, match="path"):
                await connector.query(ConnectorQuery(resource="file", filters={}))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    attrs = span.attributes or {}
    assert attrs.get("connector.operation") == "query"
    assert attrs.get("connector.error_type") == "ValueError"
    assert span.status.status_code == StatusCode.ERROR
    assert "exception" in [event.name for event in span.events]
