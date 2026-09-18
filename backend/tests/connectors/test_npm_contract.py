"""npm connector contract tests — replayed from committed cassettes (no network).

Cassettes are in tests/cassettes/test_npm_*.yaml.  Run with VCR_RECORD_MODE=once
to re-record against the live registry.
"""

import httpx
import pytest

from modulo.connectors.base import ConnectorQuery, ConnectorType


@pytest.mark.vcr
async def test_npm_search_express(npm_connector):
    """Search for 'express' on npm — replayed from cassette."""
    result = await npm_connector.query(ConnectorQuery(resource="search", filters={"text": "express"}, limit=5))
    assert result.records
    pkg = result.records[0]
    assert "name" in pkg
    assert "version" in pkg


@pytest.mark.vcr
async def test_npm_get_package(npm_connector):
    """Fetch package metadata for 'express' — replayed from cassette."""
    result = await npm_connector.query(ConnectorQuery(resource="package", filters={"package": "express"}))
    assert len(result.records) == 1
    body = result.records[0]
    assert body.get("name") == "express"
    assert "description" in body


def test_npm_connector_type(npm_connector):
    """npm connector reports correct type."""
    assert npm_connector.connector_type == ConnectorType.NPM


async def test_npm_health_check(npm_connector, monkeypatch):
    """Health check reports registry reachability — stubbed offline via MockTransport."""
    from modulo.connectors.base import HealthResult

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": []})

    monkeypatch.setattr(
        npm_connector,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://registry.npmjs.org",
        ),
    )

    result = await npm_connector.health_check()
    assert isinstance(result, HealthResult)
    assert result.ok is True
    assert result.detail == "npm registry reachable"


async def test_npm_write_raises(npm_connector):
    """npm connector is read-only — write() must raise."""
    from modulo.connectors.base import ConnectorPayload

    with pytest.raises(ValueError, match="read-only"):
        await npm_connector.write(ConnectorPayload(resource="package", data={"name": "test"}))


async def test_npm_search_requires_text(npm_connector):
    """Search without 'text' filter raises ValueError."""
    with pytest.raises(ValueError, match="requires 'text'"):
        await npm_connector.query(ConnectorQuery(resource="search", filters={}))


async def test_npm_package_requires_name(npm_connector):
    """Package query without 'package' filter raises ValueError."""
    with pytest.raises(ValueError, match="requires 'package'"):
        await npm_connector.query(ConnectorQuery(resource="package", filters={}))


async def test_npm_unsupported_resource(npm_connector):
    """Query for an unsupported resource raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported npm resource"):
        await npm_connector.query(ConnectorQuery(resource="nonexistent_resource", filters={"x": "y"}))
