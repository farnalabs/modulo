"""Unit tests for NpmConnector — HTTP responses are mocked via respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.npm import NpmConnector

API_BASE = "https://registry.npmjs.org"


@pytest.fixture
def connector():
    return NpmConnector()


@pytest.fixture
def connector_with_token():
    return NpmConnector(token="npm_test_token")


# --- connector_type ---


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.NPM


def test_connector_type_capabilities():
    caps = ConnectorType.NPM.capabilities
    assert "package_management" in caps
    assert "read" in caps


# --- health_check ---


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "express", "size": 1}).mock(
        return_value=httpx.Response(200, json={"objects": [], "total": 0}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "reachable" in result.detail


@respx.mock
async def test_health_check_unauthorized(connector_with_token):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "express", "size": 1}).mock(
        return_value=httpx.Response(401, text="Unauthorized"),
    )
    result = await connector_with_token.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_forbidden(connector_with_token):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "express", "size": 1}).mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    result = await connector_with_token.health_check()
    assert result.ok is False
    assert "permissions" in result.detail


@respx.mock
async def test_health_check_connection_error(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "express", "size": 1}).mock(
        side_effect=httpx.ConnectError("connection refused"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect" in result.detail


@respx.mock
async def test_health_check_generic_error(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "express", "size": 1}).mock(
        side_effect=ValueError("weird error"),
    )
    result = await connector.health_check()
    assert result.ok is False


@respx.mock
async def test_health_check_other_status(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "express", "size": 1}).mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


# --- query: package ---


@respx.mock
async def test_query_package(connector):
    respx.get(f"{API_BASE}/express").mock(
        return_value=httpx.Response(
            200,
            json={"name": "express", "version": "4.18.2", "description": "Fast, unopinionated framework"},
        ),
    )
    result = await connector.query(ConnectorQuery(resource="package", filters={"package": "express"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "express"
    assert result.total == 1


@respx.mock
async def test_query_package_missing_filter(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="package"))


# --- query: package_version ---


@respx.mock
async def test_query_package_version(connector):
    respx.get(f"{API_BASE}/express/4.18.2").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "express",
                "version": "4.18.2",
                "dist": {"tarball": "https://registry.npmjs.org/express/4.18.2.tgz"},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="package_version", filters={"package": "express", "version": "4.18.2"})
    )
    assert len(result.records) == 1
    assert result.records[0]["version"] == "4.18.2"


@respx.mock
async def test_query_package_version_missing_package(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="package_version", filters={"version": "4.18.2"}))


@respx.mock
async def test_query_package_version_missing_version(connector):
    with pytest.raises(ValueError, match="requires 'version' in filters"):
        await connector.query(ConnectorQuery(resource="package_version", filters={"package": "express"}))


# --- query: search ---


@respx.mock
async def test_query_search(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "objects": [
                    {"package": {"name": "react", "version": "18.2.0"}},
                    {"package": {"name": "react-dom", "version": "18.2.0"}},
                ],
                "total": 2,
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "react"
    assert result.total == 2


@respx.mock
async def test_query_search_empty(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "nonexistent-package-xyz", "size": "100"}).mock(
        return_value=httpx.Response(200, json={"objects": [], "total": 0}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "nonexistent-package-xyz"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_search_non_list_objects_no_crash(connector):
    """A corrupt ``objects`` field must fall back to an empty page, not a bare string."""
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100"}).mock(
        return_value=httpx.Response(200, json={"objects": "not-a-list", "total": 0}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_search_corrupt_body_no_crash(connector):
    """A non-dict search body must degrade to an empty page, not crash on ``.get()``."""
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100"}).mock(
        return_value=httpx.Response(200, json=["garbage"]),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_search_with_limit(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "5"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "objects": [
                    {"package": {"name": "react", "version": "18.2.0"}},
                ],
                "total": 1,
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=5))
    assert len(result.records) == 1


@respx.mock
async def test_query_search_with_from(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100", "from": "20"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "objects": [
                    {"package": {"name": "react", "version": "18.2.0"}},
                ],
                "total": 100,
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react", "from": 20}))
    assert len(result.records) == 1


@respx.mock
async def test_query_search_missing_text(connector):
    with pytest.raises(ValueError, match="requires 'text' in filters"):
        await connector.query(ConnectorQuery(resource="search"))


# --- query: package_files ---


@respx.mock
async def test_query_package_files(connector):
    respx.get(f"{API_BASE}/express/4.18.2/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"path": "index.js", "size": 1024},
                {"path": "package.json", "size": 512},
                {"path": "README.md", "size": 2048},
            ],
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="package_files", filters={"package": "express", "version": "4.18.2"})
    )
    assert len(result.records) == 3
    assert result.total == 3
    assert result.records[0]["path"] == "index.js"


@respx.mock
async def test_query_package_files_missing_package(connector):
    with pytest.raises(ValueError, match="requires 'package' in filters"):
        await connector.query(ConnectorQuery(resource="package_files", filters={"version": "4.18.2"}))


@respx.mock
async def test_query_package_files_missing_version(connector):
    with pytest.raises(ValueError, match="requires 'version' in filters"):
        await connector.query(ConnectorQuery(resource="package_files", filters={"package": "express"}))


# --- query: scope_packages ---


@respx.mock
async def test_query_scope_packages(connector):
    respx.get(f"{API_BASE}/-/v1/search", params={"scope": "@angular", "size": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "objects": [
                    {"package": {"name": "@angular/core", "version": "16.0.0"}},
                    {"package": {"name": "@angular/common", "version": "16.0.0"}},
                ],
                "total": 2,
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="scope_packages", filters={"scope": "@angular"}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "@angular/core"


@respx.mock
async def test_query_scope_packages_missing_scope(connector):
    with pytest.raises(ValueError, match="requires 'scope' in filters"):
        await connector.query(ConnectorQuery(resource="scope_packages"))


# --- query: search pagination (next_cursor / cursor) ---


@respx.mock
async def test_query_search_next_cursor_when_more_results(connector):
    objects = [{"package": {"name": f"pkg-{i}", "version": "1.0.0"}} for i in range(10)]
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "10"}).mock(
        return_value=httpx.Response(200, json={"objects": objects, "total": 100}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10))
    assert len(result.records) == 10
    assert result.total == 100
    assert result.next_cursor == "10"


@respx.mock
async def test_query_search_next_cursor_none_on_last_page(connector):
    objects = [{"package": {"name": f"pkg-{i}", "version": "1.0.0"}} for i in range(3)]
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100"}).mock(
        return_value=httpx.Response(200, json={"objects": objects, "total": 3}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}))
    assert len(result.records) == 3
    assert result.total == 3
    assert result.next_cursor is None


@respx.mock
async def test_query_search_cursor_forwarded_to_from(connector):
    objects = [{"package": {"name": f"pkg-{i}", "version": "1.0.0"}} for i in range(10)]
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "10", "from": "20"}).mock(
        return_value=httpx.Response(200, json={"objects": objects, "total": 100}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=10, cursor="20"))
    assert len(result.records) == 10
    assert result.next_cursor == "30"


@respx.mock
async def test_query_search_from_filter_forwarded(connector):
    objects = [{"package": {"name": "pkg", "version": "1.0.0"}} for _ in range(2)]
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100", "from": "20"}).mock(
        return_value=httpx.Response(200, json={"objects": objects, "total": 100}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react", "from": "20"}))
    assert result.next_cursor == "22"


@respx.mock
async def test_query_search_invalid_cursor_raises(connector):
    with pytest.raises(ValueError, match="cursor must be a numeric offset"):
        await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, cursor="not-a-number"))


@respx.mock
async def test_query_search_invalid_from_filter_raises(connector):
    with pytest.raises(ValueError, match="filter 'from' must be a numeric offset"):
        await connector.query(ConnectorQuery(resource="search", filters={"text": "react", "from": "abc"}))


@respx.mock
async def test_query_search_size_clamped_to_registry_max(connector):
    objects = [{"package": {"name": f"pkg-{i}", "version": "1.0.0"}} for i in range(250)]
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "250"}).mock(
        return_value=httpx.Response(200, json={"objects": objects, "total": 10000}),
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}, limit=500))
    assert len(result.records) == 250
    assert result.next_cursor == "250"


@respx.mock
async def test_query_scope_packages_cursor_pagination(connector):
    objects = [{"package": {"name": f"@scope/pkg-{i}", "version": "1.0.0"}} for i in range(10)]
    respx.get(f"{API_BASE}/-/v1/search", params={"scope": "@angular", "size": "10", "from": "10"}).mock(
        return_value=httpx.Response(200, json={"objects": objects, "total": 25}),
    )
    result = await connector.query(
        ConnectorQuery(resource="scope_packages", filters={"scope": "@angular"}, limit=10, cursor="10")
    )
    assert len(result.records) == 10
    assert result.next_cursor == "20"


# --- query: unsupported resource ---


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported npm resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


@respx.mock
async def test_query_search_non_finite_total_does_not_crash(connector):
    """A corrupt 'total: 1e999' (json parses to inf) must not crash pagination."""
    respx.get(f"{API_BASE}/-/v1/search", params={"text": "react", "size": "100"}).mock(
        return_value=httpx.Response(
            200,
            text='{"objects": [{"package": {"name": "pkg", "version": "1.0.0"}}], "total": 1e999}',
        )
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"text": "react"}))
    assert len(result.records) == 1
    assert result.total == 1
    assert result.next_cursor is None


# --- write ---


async def test_write_raises_error(connector):
    with pytest.raises(ValueError, match="npm registry is read-only"):
        await connector.write(ConnectorPayload(resource="package", data={}))


# --- constructor ---


def test_constructor_defaults():
    c = NpmConnector()
    assert not c._token


def test_constructor_with_token():
    c = NpmConnector(token="abc123")
    assert c._token == "abc123"
