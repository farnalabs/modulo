"""Unit tests for PyPIConnector — HTTP responses are mocked via respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.pypi import PyPIConnector

API_BASE = "https://pypi.org/pypi"


@pytest.fixture()
def connector():
    return PyPIConnector()


@pytest.fixture()
def connector_with_token():
    return PyPIConnector(token="pypi_test_token")


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.PYPI


def test_connector_type_capabilities():
    caps = ConnectorType.PYPI.capabilities
    assert "package_management" in caps
    assert "read" in caps


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{API_BASE}/").mock(
        return_value=httpx.Response(200, text="PyPI"),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "reachable" in result.detail


@respx.mock
async def test_health_check_unauthorized(connector_with_token):
    respx.get(f"{API_BASE}/").mock(
        return_value=httpx.Response(401, text="Unauthorized"),
    )
    result = await connector_with_token.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_forbidden(connector_with_token):
    respx.get(f"{API_BASE}/").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    result = await connector_with_token.health_check()
    assert result.ok is False
    assert "permissions" in result.detail


@respx.mock
async def test_health_check_connection_error(connector):
    respx.get(f"{API_BASE}/").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect" in result.detail


@respx.mock
async def test_health_check_generic_error(connector):
    respx.get(f"{API_BASE}/").mock(
        side_effect=ValueError("weird error"),
    )
    result = await connector.health_check()
    assert result.ok is False


@respx.mock
async def test_health_check_other_status(connector):
    respx.get(f"{API_BASE}/").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_query_package(connector):
    respx.get(f"{API_BASE}/requests/json").mock(
        return_value=httpx.Response(
            200,
            json={"info": {"name": "requests", "version": "2.31.0"}, "releases": {}},
        ),
    )
    result = await connector.query(ConnectorQuery(resource="package", filters={"package": "requests"}))
    assert len(result.records) == 1
    assert result.records[0]["info"]["name"] == "requests"
    assert result.total == 1


@respx.mock
async def test_query_package_missing_filter(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="package"))


@respx.mock
async def test_query_package_version(connector):
    respx.get(f"{API_BASE}/requests/2.31.0/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "info": {"name": "requests", "version": "2.31.0"},
                "releases": {
                    "2.31.0": [
                        {
                            "filename": "requests-2.31.0.tar.gz",
                            "url": "https://files.pythonhosted.org/packages/requests-2.31.0.tar.gz",
                        }
                    ]
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="package_version", filters={"package": "requests", "version": "2.31.0"})
    )
    assert len(result.records) == 1
    assert result.records[0]["info"]["version"] == "2.31.0"


@respx.mock
async def test_query_package_version_missing_package(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="package_version", filters={"version": "2.31.0"}))


@respx.mock
async def test_query_package_version_missing_version(connector):
    with pytest.raises(ValueError, match="requires 'version' in filters"):
        await connector.query(ConnectorQuery(resource="package_version", filters={"package": "requests"}))


@respx.mock
async def test_query_search(connector):
    xml_resp = """<?xml version='1.0'?>
<methodResponse>
  <params>
    <param>
      <value>
        <array>
          <data>
            <value>
              <struct>
                <member><name>name</name><value><string>aiohttp</string></value></member>
                <member><name>version</name><value><string>3.9.0</string></value></member>
                <member><name>summary</name><value><string>Async HTTP client/server</string></value></member>
              </struct>
            </value>
            <value>
              <struct>
                <member><name>name</name><value><string>asyncio</string></value></member>
                <member><name>version</name><value><string>3.4.3</string></value></member>
                <member><name>summary</name>
                  <value><string>Reference implementation of PEP 3156</string></value>
                </member>
              </struct>
            </value>
          </data>
        </array>
      </value>
    </param>
  </params>
</methodResponse>"""
    respx.post(f"{API_BASE}/").mock(
        return_value=httpx.Response(200, text=xml_resp),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "asyncio"}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "aiohttp"


@respx.mock
async def test_query_search_empty(connector):
    xml_resp = """<?xml version='1.0'?>
<methodResponse>
  <params>
    <param>
      <value><array><data></data></array></value>
    </param>
  </params>
</methodResponse>"""
    respx.post(f"{API_BASE}/").mock(
        return_value=httpx.Response(200, text=xml_resp),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "nonexistent-package-xyz"}))
    assert len(result.records) == 0
    assert result.total == 0


@respx.mock
async def test_query_search_missing_text(connector):
    with pytest.raises(ValueError, match="requires 'text' in filters"):
        await connector.query(ConnectorQuery(resource="search"))


@respx.mock
async def test_query_package_files(connector):
    respx.get(f"{API_BASE}/requests/2.31.0/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "info": {"name": "requests", "version": "2.31.0"},
                "releases": {
                    "2.31.0": [
                        {
                            "filename": "requests-2.31.0.tar.gz",
                            "size": 102400,
                            "url": "https://files.pythonhosted.org/packages/requests-2.31.0.tar.gz",
                        },
                        {
                            "filename": "requests-2.31.0-py3-none-any.whl",
                            "size": 51200,
                            "url": "https://files.pythonhosted.org/packages/requests-2.31.0-py3-none-any.whl",
                        },
                    ]
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="package_files", filters={"package": "requests", "version": "2.31.0"})
    )
    assert len(result.records) == 2
    assert result.total == 2
    assert result.records[0]["filename"] == "requests-2.31.0.tar.gz"


@respx.mock
async def test_query_package_files_missing_package(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="package_files", filters={"version": "2.31.0"}))


@respx.mock
async def test_query_package_files_missing_version(connector):
    with pytest.raises(ValueError, match="requires 'version' in filters"):
        await connector.query(ConnectorQuery(resource="package_files", filters={"package": "requests"}))


@respx.mock
async def test_query_simple_list(connector):
    respx.get(f"{API_BASE}/requests/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "info": {"name": "requests", "version": "2.31.0"},
                "releases": {
                    "2.31.0": [{"filename": "requests-2.31.0.tar.gz"}],
                    "2.30.0": [{"filename": "requests-2.30.0.tar.gz"}],
                    "2.29.0": [{"filename": "requests-2.29.0.tar.gz"}],
                },
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="simple_list", filters={"package": "requests"}))
    assert len(result.records) == 1
    assert result.total == 3
    versions = result.records[0]["versions"]
    assert "2.31.0" in versions
    assert len(versions) == 3


@respx.mock
async def test_query_simple_list_missing_package(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="simple_list"))


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported PyPI resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_write_raises_error(connector):
    with pytest.raises(ValueError, match="PyPI registry is read-only"):
        await connector.write(ConnectorPayload(resource="package", data={}))


def test_constructor_defaults():
    c = PyPIConnector()
    assert c._token == ""


def test_constructor_with_token():
    c = PyPIConnector(token="abc123")
    assert c._token == "abc123"
