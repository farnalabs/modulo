"""Unit tests for SnykConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.snyk import SnykConnector

TOKEN = "snyk_test_token"
_BASE = "https://api.snyk.io/rest"
ORG = "org-1"
PROJECT = "proj-1"


@pytest.fixture
def connector():
    return SnykConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SNYK


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/orgs").mock(return_value=httpx.Response(200, json={"data": []}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/orgs").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Snyk auth token" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    respx.get(f"{_BASE}/orgs").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "lacks required permissions" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/orgs").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect to Snyk API" in result.detail


# ---------------------------------------------------------------------------
# query — projects
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    body = {
        "data": [{"id": PROJECT, "attributes": {"name": "my-project"}}],
        "meta": {"count": 1},
    }
    respx.get(f"{_BASE}/orgs/{ORG}/projects").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="projects", filters={"org_id": ORG}, limit=10),
    )
    assert result.total == 1
    assert result.records[0]["id"] == PROJECT


@respx.mock
async def test_query_projects_with_cursor(connector):
    body = {
        "data": [{"id": PROJECT}],
        "meta": {"count": 1},
        "links": {"next": "https://api.snyk.io/rest/orgs/org-1/projects?starting_after=x"},
    }
    respx.get(f"{_BASE}/orgs/{ORG}/projects").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="projects", filters={"org_id": ORG}, cursor="abc", limit=10),
    )
    assert result.next_cursor is not None


async def test_query_projects_missing_org(connector):
    query = ConnectorQuery(resource="projects", limit=10)
    with pytest.raises(ValueError, match="'org_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — project
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_project(connector):
    body = {"data": {"id": PROJECT, "attributes": {"name": "my-project"}}}
    respx.get(f"{_BASE}/orgs/{ORG}/projects/{PROJECT}").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="project", filters={"org_id": ORG, "project_id": PROJECT}),
    )
    assert result.records[0]["id"] == PROJECT


async def test_query_project_missing_filters(connector):
    query = ConnectorQuery(resource="project", filters={"project_id": PROJECT})
    with pytest.raises(ValueError, match="'org_id' in filters"):
        await connector.query(query)
    query = ConnectorQuery(resource="project", filters={"org_id": ORG})
    with pytest.raises(ValueError, match="'project_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    body = {
        "data": [{"id": "SNYK-123", "attributes": {"title": "SQL injection"}}],
        "meta": {"count": 1},
    }
    respx.get(f"{_BASE}/orgs/{ORG}/projects/{PROJECT}/issues").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={"org_id": ORG, "project_id": PROJECT, "severity": "high"},
            limit=10,
        ),
    )
    assert result.total == 1


async def test_query_issues_missing_filters(connector):
    query = ConnectorQuery(resource="issues", filters={"project_id": PROJECT})
    with pytest.raises(ValueError, match="'org_id' in filters"):
        await connector.query(query)
    query = ConnectorQuery(resource="issues", filters={"org_id": ORG})
    with pytest.raises(ValueError, match="'project_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — aggregated_issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_aggregated_issues(connector):
    body = {"data": [{"id": "SNYK-456"}]}
    respx.post(f"{_BASE}/orgs/{ORG}/packages/issues").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(
            resource="aggregated_issues",
            filters={"org_id": ORG, "packages": [{"name": "requests", "version": "4.0.0", "ecosystem": "pypi"}]},
        ),
    )
    assert result.total == 1


async def test_query_aggregated_issues_missing_packages(connector):
    with pytest.raises(ValueError, match="'packages' in filters"):
        await connector.query(
            ConnectorQuery(resource="aggregated_issues", filters={"org_id": ORG}),
        )


# ---------------------------------------------------------------------------
# query — orgs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_orgs(connector):
    body = {"data": [{"id": ORG, "attributes": {"name": "my-org"}}], "meta": {"count": 1}}
    respx.get(f"{_BASE}/orgs").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="orgs", limit=10))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_tests(connector):
    body = {"data": [{"id": "test-1"}], "meta": {"count": 1}}
    respx.get(f"{_BASE}/orgs/{ORG}/tests").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="tests", filters={"org_id": ORG}, limit=10))
    assert result.total == 1


async def test_query_tests_missing_org(connector):
    with pytest.raises(ValueError, match="'org_id' in filters"):
        await connector.query(ConnectorQuery(resource="tests", limit=10))


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Snyk resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — test
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_test(connector):
    created = {"data": {"id": "test-new"}}
    respx.post(f"{_BASE}/orgs/{ORG}/tests").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="test",
            data={"org_id": ORG, "name": "requests", "version": "4.0.0", "ecosystem": "pypi"},
        ),
    )
    assert result["data"]["id"] == "test-new"


async def test_write_test_missing_fields(connector):
    with pytest.raises(ValueError, match="'org_id' in data"):
        await connector.write(ConnectorPayload(resource="test", data={"name": "requests"}))
    with pytest.raises(ValueError, match="'name', 'version', and 'ecosystem' in data"):
        await connector.write(
            ConnectorPayload(resource="test", data={"org_id": ORG, "name": "requests"}),
        )


# ---------------------------------------------------------------------------
# write — ignore
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_ignore(connector):
    created = {"data": {"id": "SNYK-123"}}
    respx.post(f"{_BASE}/orgs/{ORG}/projects/{PROJECT}/issues/SNYK-123/ignore").mock(
        return_value=httpx.Response(201, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="ignore",
            data={"org_id": ORG, "project_id": PROJECT, "issue_id": "SNYK-123", "reason": "test"},
        ),
    )
    assert result["data"]["id"] == "SNYK-123"


async def test_write_ignore_missing_fields(connector):
    with pytest.raises(ValueError, match="'org_id' in data"):
        await connector.write(
            ConnectorPayload(resource="ignore", data={"project_id": PROJECT, "issue_id": "SNYK-123"}),
        )
    with pytest.raises(ValueError, match="'project_id' in data"):
        await connector.write(
            ConnectorPayload(resource="ignore", data={"org_id": ORG, "issue_id": "SNYK-123"}),
        )
    with pytest.raises(ValueError, match="'issue_id' in data"):
        await connector.write(
            ConnectorPayload(resource="ignore", data={"org_id": ORG, "project_id": PROJECT}),
        )


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Snyk write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/orgs/{ORG}/projects").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="projects", filters={"org_id": ORG}, limit=10))
