"""Unit tests for NpmConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.npm import NpmConnector

TOKEN = "npm_test_token"
_BASE = "https://registry.npmjs.org"


@pytest.fixture
def connector():
    return NpmConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.NPM


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/-/v1/search").mock(
        return_value=httpx.Response(200, json={"objects": [], "total": 0}),
    )
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid npm auth token" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "lacks required permissions" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/-/v1/search").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect to npm registry" in result.detail


# ---------------------------------------------------------------------------
# query — package
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_package(connector):
    body = {"name": "express", "dist-tags": {"latest": "4.18.2"}}
    respx.get(f"{_BASE}/express").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="package", filters={"package": "express"}))
    assert result.total == 1
    assert result.records[0]["name"] == "express"


async def test_query_package_missing_filter(connector):
    query = ConnectorQuery(resource="package")
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — package_version
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_package_version(connector):
    body = {"name": "express", "version": "4.18.2"}
    respx.get(f"{_BASE}/express/4.18.2").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="package_version", filters={"package": "express", "version": "4.18.2"}),
    )
    assert result.records[0]["version"] == "4.18.2"


async def test_query_package_version_missing_package(connector):
    query = ConnectorQuery(resource="package_version", filters={"version": "4.18.2"})
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(query)


async def test_query_package_version_missing_version(connector):
    query = ConnectorQuery(resource="package_version", filters={"package": "express"})
    with pytest.raises(ValueError, match="'version' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — search
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_search(connector):
    body = {
        "objects": [
            {"package": {"name": "react", "version": "18.2.0"}},
            {"package": {"name": "react-dom", "version": "18.2.0"}},
        ],
        "total": 2,
    }
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10))
    assert result.total == 2
    assert result.records[0]["name"] == "react"


@respx.mock
async def test_query_search_malformed_object_not_echoed_as_package(connector):
    """A search object without a 'package' key must not be echoed as the record.

    Regression: `o.get("package", o)` silently returned the whole search
    object (including score/searchScore metadata) as the package record.
    """
    body = {"objects": [{"package": {"name": "react", "version": "18.2.0"}}, {"name": "malformed"}], "total": 2}
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10))
    assert result.records[0]["name"] == "react"
    assert not result.records[1]


@respx.mock
async def test_query_search_with_from_offset(connector):
    body = {"objects": [{"package": {"name": "react-helmet", "version": "6.1.0"}}], "total": 1}
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="search", filters={"text": "react", "from": 20}, limit=10),
    )
    assert result.total == 1


@respx.mock
async def test_query_search_with_cursor(connector):
    body = {"objects": [], "total": 0}
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, cursor="25"))
    assert result.total == 0


async def test_query_search_missing_text(connector):
    query = ConnectorQuery(resource="search")
    with pytest.raises(ValueError, match="'text' in filters"):
        await connector.query(query)


@respx.mock
async def test_query_search_non_list_objects_no_crash(connector):
    """A corrupt body placing a non-list in ``objects`` must fall back to an empty result."""
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json={"objects": "corrupt", "total": 1}))
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10))
    assert not result.records
    assert result.total == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_search_non_dict_body_no_crash(connector):
    """A corrupt/hostile non-dict body must degrade to an empty result."""
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=["not-a-dict"]))
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_search_non_dict_object_skipped(connector):
    """A non-dict ``objects`` element must be skipped instead of crashing the package extraction."""
    body = {"objects": [{"package": {"name": "react", "version": "18.2.0"}}, "corrupt"], "total": 2}
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10))
    assert result.records == [{"name": "react", "version": "18.2.0"}]


# ---------------------------------------------------------------------------
# query — package_files
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_package_files(connector):
    files = [{"path": "index.js", "size": 1024}]
    respx.get(f"{_BASE}/express/4.18.2/files").mock(return_value=httpx.Response(200, json=files))
    result = await connector.query(
        ConnectorQuery(resource="package_files", filters={"package": "express", "version": "4.18.2"}),
    )
    assert result.total == 1
    assert result.records[0]["path"] == "index.js"


async def test_query_package_files_missing_filters(connector):
    query = ConnectorQuery(resource="package_files", filters={"version": "4.18.2"})
    with pytest.raises(ValueError, match="'package' in filters"):
        await connector.query(query)
    with pytest.raises(ValueError, match="'version' in filters"):
        await connector.query(ConnectorQuery(resource="package_files", filters={"package": "express"}))


# ---------------------------------------------------------------------------
# query — scope_packages
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_scope_packages(connector):
    body = {
        "objects": [{"package": {"name": "@angular/core", "version": "17.0.0"}}],
        "total": 1,
    }
    respx.get(f"{_BASE}/-/v1/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="scope_packages", filters={"scope": "@angular"}, limit=10),
    )
    assert result.total == 1
    assert result.records[0]["name"] == "@angular/core"


async def test_query_scope_packages_missing_scope(connector):
    with pytest.raises(ValueError, match="'scope' in filters"):
        await connector.query(ConnectorQuery(resource="scope_packages"))


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported npm resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — read-only
# ---------------------------------------------------------------------------


async def test_write_raises_read_only(connector):
    with pytest.raises(ValueError, match="read-only"):
        await connector.write(ConnectorPayload(resource="package", data={"package": "express"}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/express").mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="package", filters={"package": "express"}))
