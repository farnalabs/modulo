"""Unit tests for PyPIConnector — HTTP responses are mocked via httpx + respx."""

import xmlrpc.client

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.pypi import PyPIConnector

TOKEN = "pypi_test_token"
_BASE = "https://pypi.org/pypi"


@pytest.fixture
def connector():
    return PyPIConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.PYPI


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(200, text="PyPI"))
    result = await connector.health_check()
    assert result.ok is True
    assert "reachable" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid PyPI auth token" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "lacks required permissions" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect to PyPI registry" in result.detail


# ---------------------------------------------------------------------------
# query — package
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_package(connector):
    body = {"info": {"name": "requests", "version": "2.31.0"}}
    respx.get(f"{_BASE}/requests/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="package", filters={"package": "requests"}))
    assert result.total == 1
    assert result.records[0]["info"]["name"] == "requests"


async def test_query_package_missing_filter(connector):
    query = ConnectorQuery(resource="package")
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — package_version
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_package_version(connector):
    body = {"info": {"name": "requests", "version": "2.31.0"}}
    respx.get(f"{_BASE}/requests/2.31.0/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="package_version", filters={"package": "requests", "version": "2.31.0"}),
    )
    assert result.records[0]["info"]["version"] == "2.31.0"


async def test_query_package_version_missing_package(connector):
    query = ConnectorQuery(resource="package_version", filters={"version": "2.31.0"})
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(query)


async def test_query_package_version_missing_version(connector):
    query = ConnectorQuery(resource="package_version", filters={"package": "requests"})
    with pytest.raises(ValueError, match="'version' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — search (XML-RPC)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_search(connector):
    xml_results = [
        {"name": "aiohttp", "version": "3.9.0", "summary": "Async HTTP client"},
        {"name": "asyncio", "version": "3.4.3", "summary": "Async IO"},
    ]
    body = xmlrpc.client.dumps((xml_results,), methodresponse=True)
    respx.post(f"{_BASE}/").mock(
        return_value=httpx.Response(200, text=body, headers={"Content-Type": "text/xml"}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "asyncio"}, limit=10))
    assert result.total == 2
    assert result.records[0]["name"] == "aiohttp"


@respx.mock
async def test_query_search_with_operator(connector):
    xml_results = [{"name": "asyncio", "version": "3.4.3", "summary": ""}]
    body = xmlrpc.client.dumps((xml_results,), methodresponse=True)
    respx.post(f"{_BASE}/").mock(
        return_value=httpx.Response(200, text=body, headers={"Content-Type": "text/xml"}),
    )
    result = await connector.query(
        ConnectorQuery(resource="search", filters={"text": "asyncio", "operator": "or"}, limit=10),
    )
    assert result.total == 1


async def test_query_search_missing_text(connector):
    query = ConnectorQuery(resource="search")
    with pytest.raises(ValueError, match="'text' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — package_files
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_package_files(connector):
    body = {
        "info": {"name": "requests", "version": "2.31.0"},
        "releases": {
            "2.31.0": [{"filename": "requests-2.31.0-py3-none-any.whl"}],
            "2.30.0": [{"filename": "requests-2.30.0.tar.gz"}],
        },
    }
    respx.get(f"{_BASE}/requests/2.31.0/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="package_files", filters={"package": "requests", "version": "2.31.0"}),
    )
    assert result.total == 1
    assert result.records[0]["filename"] == "requests-2.31.0-py3-none-any.whl"


async def test_query_package_files_missing_filters(connector):
    query = ConnectorQuery(resource="package_files", filters={"version": "2.31.0"})
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(query)
    with pytest.raises(ValueError, match="'version' in filters"):
        await connector.query(ConnectorQuery(resource="package_files", filters={"package": "requests"}))


# ---------------------------------------------------------------------------
# query — simple_list
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_simple_list(connector):
    body = {
        "info": {"name": "requests"},
        "releases": {
            "2.30.0": [],
            "2.31.0": [],
            "2.29.0": [],
        },
    }
    respx.get(f"{_BASE}/requests/json").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="simple_list", filters={"package": "requests"}))
    assert result.total == 3
    assert result.records[0]["versions"] == ["2.31.0", "2.30.0", "2.29.0"]


async def test_query_simple_list_missing_package(connector):
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(ConnectorQuery(resource="simple_list"))


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported PyPI resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — read-only
# ---------------------------------------------------------------------------


async def test_write_raises_read_only(connector):
    with pytest.raises(ValueError, match="read-only"):
        await connector.write(ConnectorPayload(resource="package", data={"package": "requests"}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/requests/json").mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="package", filters={"package": "requests"}))
