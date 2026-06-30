"""Unit tests for SnykConnector — HTTP responses are mocked via respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.snyk import SnykConnector

TOKEN = "snyk_test_token"
API_BASE = "https://api.snyk.io/rest"
VERSION = "2024-10-15"


@pytest.fixture()
def connector():
    return SnykConnector(token=TOKEN)


# --- health_check ---


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{API_BASE}/orgs", params={"limit": 1, "version": VERSION}).mock(
        return_value=httpx.Response(200, json={"data": []}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_unauthorized(connector):
    respx.get(f"{API_BASE}/orgs", params={"limit": 1, "version": VERSION}).mock(
        return_value=httpx.Response(401, text="Unauthorized"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    respx.get(f"{API_BASE}/orgs", params={"limit": 1, "version": VERSION}).mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "permissions" in result.detail


@respx.mock
async def test_health_check_connection_error(connector):
    respx.get(f"{API_BASE}/orgs", params={"limit": 1, "version": VERSION}).mock(
        side_effect=httpx.ConnectError("connection refused"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect" in result.detail


@respx.mock
async def test_health_check_generic_error(connector):
    respx.get(f"{API_BASE}/orgs", params={"limit": 1, "version": VERSION}).mock(
        side_effect=ValueError("weird error"),
    )
    result = await connector.health_check()
    assert result.ok is False


@respx.mock
async def test_health_check_other_status(connector):
    respx.get(f"{API_BASE}/orgs", params={"limit": 1, "version": VERSION}).mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


# --- connector_type ---


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SNYK


# --- query: projects ---


@respx.mock
async def test_query_projects(connector):
    projects = [
        {"id": "proj-1", "attributes": {"name": "my-app", "origin": "github"}},
        {"id": "proj-2", "attributes": {"name": "other-app", "origin": "cli"}},
    ]
    respx.get(f"{API_BASE}/orgs/my-org/projects", params={"version": VERSION, "limit": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": projects,
                "meta": {"count": 2},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="projects",
            filters={"org_id": "my-org"},
            limit=100,
        )
    )
    assert len(result.records) == 2
    assert result.records[0]["id"] == "proj-1"
    assert result.total == 2


@respx.mock
async def test_query_projects_with_names(connector):
    respx.get(f"{API_BASE}/orgs/my-org/projects", params={"version": VERSION, "limit": "100", "names": "my-app"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "proj-1", "attributes": {"name": "my-app"}}],
                "meta": {"count": 1},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="projects",
            filters={"org_id": "my-org", "names": "my-app"},
            limit=100,
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_projects_with_cursor(connector):
    respx.get(
        f"{API_BASE}/orgs/my-org/projects",
        params={"version": VERSION, "limit": "100", "starting_after": "next-token"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "proj-3"}],
                "meta": {"count": 1},
                "links": {"next": "next-page-token"},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="projects",
            filters={"org_id": "my-org"},
            limit=100,
            cursor="next-token",
        )
    )
    assert len(result.records) == 1
    assert result.next_cursor == "next-page-token"


@respx.mock
async def test_query_projects_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in filters"):
        await connector.query(ConnectorQuery(resource="projects"))


@respx.mock
async def test_query_projects_empty(connector):
    respx.get(f"{API_BASE}/orgs/my-org/projects", params={"version": VERSION, "limit": "100"}).mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"count": 0}}),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="projects",
            filters={"org_id": "my-org"},
            limit=100,
        )
    )
    assert len(result.records) == 0
    assert result.total == 0


# --- query: project (single) ---


@respx.mock
async def test_query_project(connector):
    respx.get(f"{API_BASE}/orgs/my-org/projects/proj-1", params={"version": VERSION}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"id": "proj-1", "attributes": {"name": "my-app"}},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="project",
            filters={"org_id": "my-org", "project_id": "proj-1"},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "proj-1"


@respx.mock
async def test_query_project_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in filters"):
        await connector.query(
            ConnectorQuery(
                resource="project",
                filters={"project_id": "proj-1"},
            )
        )


@respx.mock
async def test_query_project_missing_project_id(connector):
    with pytest.raises(ValueError, match="requires 'project_id' in filters"):
        await connector.query(
            ConnectorQuery(
                resource="project",
                filters={"org_id": "my-org"},
            )
        )


@respx.mock
async def test_query_project_not_found(connector):
    respx.get(f"{API_BASE}/orgs/my-org/projects/nonexistent", params={"version": VERSION}).mock(
        return_value=httpx.Response(200, json={"data": {}}),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="project",
            filters={"org_id": "my-org", "project_id": "nonexistent"},
        )
    )
    assert len(result.records) == 0


# --- query: issues ---


@respx.mock
async def test_query_issues(connector):
    respx.get(
        f"{API_BASE}/orgs/my-org/projects/proj-1/issues",
        params={"version": VERSION, "limit": "100"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "SNYK-001", "attributes": {"type": "vuln"}}],
                "meta": {"count": 1},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={"org_id": "my-org", "project_id": "proj-1"},
            limit=100,
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "SNYK-001"


@respx.mock
async def test_query_issues_with_filters(connector):
    respx.get(
        f"{API_BASE}/orgs/my-org/projects/proj-1/issues",
        params={"version": VERSION, "limit": "100", "type": "vuln", "status": "open", "severity": "critical"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "SNYK-002"}],
                "meta": {"count": 1},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={
                "org_id": "my-org",
                "project_id": "proj-1",
                "types": "vuln",
                "status": "open",
                "severity": "critical",
            },
            limit=100,
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_issues_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in filters"):
        await connector.query(
            ConnectorQuery(
                resource="issues",
                filters={"project_id": "proj-1"},
            )
        )


@respx.mock
async def test_query_issues_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project_id' in filters"):
        await connector.query(
            ConnectorQuery(
                resource="issues",
                filters={"org_id": "my-org"},
            )
        )


# --- query: aggregated_issues ---


@respx.mock
async def test_query_aggregated_issues(connector):
    respx.post(f"{API_BASE}/orgs/my-org/packages/issues", params={"version": VERSION}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "SNYK-003", "attributes": {"type": "vuln", "package": "requests"}}],
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="aggregated_issues",
            filters={
                "org_id": "my-org",
                "packages": [{"name": "requests", "version": "4.0.0", "ecosystem": "pypi"}],
            },
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_aggregated_issues_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in filters"):
        await connector.query(
            ConnectorQuery(
                resource="aggregated_issues",
                filters={"packages": [{"name": "requests", "version": "4.0.0", "ecosystem": "pypi"}]},
            )
        )


@respx.mock
async def test_query_aggregated_issues_missing_packages(connector):
    with pytest.raises(ValueError, match="requires 'packages' in filters"):
        await connector.query(
            ConnectorQuery(
                resource="aggregated_issues",
                filters={"org_id": "my-org"},
            )
        )


# --- query: orgs ---


@respx.mock
async def test_query_orgs(connector):
    respx.get(f"{API_BASE}/orgs", params={"version": VERSION, "limit": "10"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "org-1", "attributes": {"name": "My Org"}}],
                "meta": {"count": 1},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="orgs", limit=10))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "org-1"


@respx.mock
async def test_query_orgs_with_cursor(connector):
    respx.get(
        f"{API_BASE}/orgs",
        params={"version": VERSION, "limit": "10", "starting_after": "cursor-token"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "org-2"}],
                "meta": {"count": 1},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="orgs", limit=10, cursor="cursor-token"))
    assert len(result.records) == 1


# --- query: tests ---


@respx.mock
async def test_query_tests(connector):
    respx.get(f"{API_BASE}/orgs/my-org/tests", params={"version": VERSION, "limit": "10"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "test-1", "attributes": {"status": "complete"}}],
                "meta": {"count": 1},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="tests",
            filters={"org_id": "my-org"},
            limit=10,
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "test-1"


@respx.mock
async def test_query_tests_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in filters"):
        await connector.query(ConnectorQuery(resource="tests"))


# --- query: unsupported resource ---


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Snyk resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# --- write: test ---


@respx.mock
async def test_write_trigger_test(connector):
    respx.post(f"{API_BASE}/orgs/my-org/tests", params={"version": VERSION}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"id": "test-1", "attributes": {"status": "running"}},
            },
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="test",
            data={"org_id": "my-org", "name": "requests", "version": "4.0.0", "ecosystem": "pypi"},
        )
    )
    assert result["data"]["id"] == "test-1"


@respx.mock
async def test_write_trigger_test_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in data"):
        await connector.write(
            ConnectorPayload(
                resource="test",
                data={"name": "requests", "version": "4.0.0", "ecosystem": "pypi"},
            )
        )


@respx.mock
async def test_write_trigger_test_missing_fields(connector):
    with pytest.raises(ValueError, match="requires 'name', 'version', and 'ecosystem' in data"):
        await connector.write(
            ConnectorPayload(
                resource="test",
                data={"org_id": "my-org", "name": "requests"},
            )
        )


# --- write: ignore ---


@respx.mock
async def test_write_ignore_issue(connector):
    respx.post(
        f"{API_BASE}/orgs/my-org/projects/proj-1/issues/SNYK-123/ignore",
        params={"version": VERSION},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"id": "SNYK-123", "attributes": {"ignored": True}},
            },
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="ignore",
            data={"org_id": "my-org", "project_id": "proj-1", "issue_id": "SNYK-123"},
        )
    )
    assert result["data"]["attributes"]["ignored"] is True


@respx.mock
async def test_write_ignore_missing_org(connector):
    with pytest.raises(ValueError, match="requires 'org_id' in data"):
        await connector.write(
            ConnectorPayload(
                resource="ignore",
                data={"project_id": "proj-1", "issue_id": "SNYK-123"},
            )
        )


@respx.mock
async def test_write_ignore_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project_id' in data"):
        await connector.write(
            ConnectorPayload(
                resource="ignore",
                data={"org_id": "my-org", "issue_id": "SNYK-123"},
            )
        )


@respx.mock
async def test_write_ignore_missing_issue(connector):
    with pytest.raises(ValueError, match="requires 'issue_id' in data"):
        await connector.write(
            ConnectorPayload(
                resource="ignore",
                data={"org_id": "my-org", "project_id": "proj-1"},
            )
        )


@respx.mock
async def test_write_ignore_with_reason(connector):
    respx.post(
        f"{API_BASE}/orgs/my-org/projects/proj-1/issues/SNYK-456/ignore",
        params={"version": VERSION},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"id": "SNYK-456", "attributes": {"ignored": True}},
            },
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="ignore",
            data={
                "org_id": "my-org",
                "project_id": "proj-1",
                "issue_id": "SNYK-456",
                "reason": "False positive",
                "reason_type": "wont-fix",
            },
        )
    )
    assert result["data"]["attributes"]["ignored"] is True


# --- write: unsupported resource ---


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Snyk write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))
