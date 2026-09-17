"""PyPI connector contract tests — replayed from committed cassettes (no network).

Cassettes are in tests/cassettes/test_pypi_*.yaml.  Run with VCR_RECORD_MODE=once
to re-record against the live registry.
"""

import httpx
import pytest

from modulo.connectors.base import ConnectorQuery, ConnectorType


@pytest.mark.vcr
async def test_pypi_get_package(pypi_connector):
    """Fetch package metadata for 'requests' — replayed from cassette."""
    result = await pypi_connector.query(ConnectorQuery(resource="package", filters={"package": "requests"}))
    assert len(result.records) == 1
    body = result.records[0]
    info = body.get("info", {})
    assert info.get("name") == "requests"


def test_pypi_connector_type(pypi_connector):
    """PyPI connector reports correct type."""
    assert pypi_connector.connector_type == ConnectorType.PYPI


async def test_pypi_health_check(pypi_connector, monkeypatch):
    """Health check reports registry reachability — stubbed offline via MockTransport."""
    from modulo.connectors.base import HealthResult

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {"name": "requests"}})

    monkeypatch.setattr(
        pypi_connector,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://pypi.org/pypi",
        ),
    )

    result = await pypi_connector.health_check()
    assert isinstance(result, HealthResult)
    assert result.ok is True
    assert result.detail == "PyPI registry reachable"


async def test_pypi_write_raises(pypi_connector):
    """PyPI connector is read-only — write() must raise."""
    from modulo.connectors.base import ConnectorPayload

    with pytest.raises(ValueError, match="read-only"):
        await pypi_connector.write(ConnectorPayload(resource="package", data={"name": "test"}))


async def test_pypi_package_requires_name(pypi_connector):
    """Package query without 'package' filter raises ValueError."""
    with pytest.raises(ValueError, match="requires 'package'"):
        await pypi_connector.query(ConnectorQuery(resource="package", filters={}))


async def test_pypi_unsupported_resource(pypi_connector):
    """Query for an unsupported resource raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported PyPI resource"):
        await pypi_connector.query(ConnectorQuery(resource="nonexistent_resource", filters={"x": "y"}))
