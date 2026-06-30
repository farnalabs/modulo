"""Unit tests for SentryConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.sentry import SentryConnector

TOKEN = "sntry_test_token"
ORG = "test-org"
_BASE = "https://sentry.io/api/0"


@pytest.fixture()
def connector() -> SentryConnector:
    return SentryConnector(token=TOKEN, organization=ORG)


def test_connector_type(connector: SentryConnector) -> None:
    assert connector.connector_type == ConnectorType.SENTRY


@respx.mock
async def test_health_check_ok(connector: SentryConnector) -> None:
    respx.get(f"{_BASE}/").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Sentry API token validated"


@respx.mock
async def test_health_check_invalid_token(connector: SentryConnector) -> None:
    respx.get(f"{_BASE}/").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_network_error(connector: SentryConnector) -> None:
    respx.get(f"{_BASE}/").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_query_issues(connector: SentryConnector) -> None:
    issues = [
        {"id": "1", "title": "Crash in login", "status": "unresolved"},
        {"id": "2", "title": "Memory leak", "status": "resolved"},
    ]
    respx.get(f"{_BASE}/projects/{ORG}/project-alpha/issues/").mock(
        return_value=httpx.Response(200, json=issues)
    )
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"project": "project-alpha"})
    )
    assert len(result.records) == 2
    assert result.records[0]["title"] == "Crash in login"


@respx.mock
async def test_query_issues_with_filters(connector: SentryConnector) -> None:
    respx.get(
        f"{_BASE}/projects/{ORG}/project-alpha/issues/",
        params={"query": "crash", "status": "unresolved", "limit": 5},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "3", "title": "Crash", "status": "unresolved"}],
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={"project": "project-alpha", "query": "crash", "status": "unresolved"},
            limit=5,
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["title"] == "Crash"


@respx.mock
async def test_query_events(connector: SentryConnector) -> None:
    events = [{"id": "e1", "eventID": "abc123", "message": "TypeError"}]
    respx.get(f"{_BASE}/projects/{ORG}/project-alpha/events/").mock(
        return_value=httpx.Response(200, json=events)
    )
    result = await connector.query(
        ConnectorQuery(resource="events", filters={"project": "project-alpha"})
    )
    assert len(result.records) == 1
    assert result.records[0]["eventID"] == "abc123"


@respx.mock
async def test_query_projects(connector: SentryConnector) -> None:
    projects = [
        {"id": "p1", "slug": "project-alpha"},
        {"id": "p2", "slug": "project-beta"},
    ]
    respx.get(f"{_BASE}/projects/").mock(
        return_value=httpx.Response(200, json=projects)
    )
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert len(result.records) == 2
    assert result.records[0]["slug"] == "project-alpha"


@respx.mock
async def test_query_releases(connector: SentryConnector) -> None:
    releases = [{"id": "r1", "version": "1.0.0"}]
    respx.get(f"{_BASE}/organizations/{ORG}/releases/").mock(
        return_value=httpx.Response(200, json=releases)
    )
    result = await connector.query(ConnectorQuery(resource="releases"))
    assert len(result.records) == 1
    assert result.records[0]["version"] == "1.0.0"


@respx.mock
async def test_query_teams(connector: SentryConnector) -> None:
    teams = [{"id": "t1", "slug": "engineering"}]
    respx.get(f"{_BASE}/organizations/{ORG}/teams/").mock(
        return_value=httpx.Response(200, json=teams)
    )
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 1
    assert result.records[0]["slug"] == "engineering"


@respx.mock
async def test_query_issue_events(connector: SentryConnector) -> None:
    issue_events = [{"id": "ie1", "eventID": "def456"}]
    respx.get(f"{_BASE}/issues/42/events/").mock(
        return_value=httpx.Response(200, json=issue_events)
    )
    result = await connector.query(
        ConnectorQuery(resource="issue_events", filters={"issue_id": "42"})
    )
    assert len(result.records) == 1
    assert result.records[0]["eventID"] == "def456"


@respx.mock
async def test_query_issue_events_missing_issue_id(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry issue_events query requires 'issue_id'"):
        await connector.query(ConnectorQuery(resource="issue_events"))


@respx.mock
async def test_write_issue_status(connector: SentryConnector) -> None:
    respx.put(f"{_BASE}/issues/42/").mock(
        return_value=httpx.Response(200, json={"id": "42", "status": "resolved"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_status",
            data={"issue_id": "42", "status": "resolved"},
        )
    )
    assert result["status"] == "resolved"


@respx.mock
async def test_write_issue_status_missing_issue_id(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry issue_status write requires 'issue_id'"):
        await connector.write(
            ConnectorPayload(resource="issue_status", data={"status": "resolved"})
        )


@respx.mock
async def test_write_issue_status_missing_status(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry issue_status write requires 'status'"):
        await connector.write(
            ConnectorPayload(resource="issue_status", data={"issue_id": "42"})
        )


@respx.mock
async def test_write_event_comment(connector: SentryConnector) -> None:
    respx.post(f"{_BASE}/issues/42/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c1", "text": "Looking into this"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="event_comment",
            data={"issue_id": "42", "text": "Looking into this"},
        )
    )
    assert result["id"] == "c1"
    assert result["text"] == "Looking into this"


@respx.mock
async def test_write_event_comment_missing_issue_id(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry event_comment write requires 'issue_id'"):
        await connector.write(
            ConnectorPayload(resource="event_comment", data={"text": "comment"})
        )


@respx.mock
async def test_write_event_comment_missing_text(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry event_comment write requires 'text'"):
        await connector.write(
            ConnectorPayload(resource="event_comment", data={"issue_id": "42"})
        )


@respx.mock
async def test_write_release(connector: SentryConnector) -> None:
    respx.post(f"{_BASE}/organizations/{ORG}/releases/").mock(
        return_value=httpx.Response(201, json={"id": "r1", "version": "2.0.0"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="release",
            data={"version": "2.0.0"},
        )
    )
    assert result["version"] == "2.0.0"


@respx.mock
async def test_write_release_with_optional_fields(connector: SentryConnector) -> None:
    respx.post(f"{_BASE}/organizations/{ORG}/releases/").mock(
        return_value=httpx.Response(201, json={"id": "r2", "version": "2.1.0", "ref": "main"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="release",
            data={"version": "2.1.0", "ref": "main", "projects": ["project-alpha"]},
        )
    )
    assert result["version"] == "2.1.0"


@respx.mock
async def test_write_release_missing_version(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry release write requires 'version'"):
        await connector.write(
            ConnectorPayload(resource="release", data={"ref": "main"})
        )


async def test_query_invalid_resource(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Sentry resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Sentry write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_issues_missing_project(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry issues query requires 'project'"):
        await connector.query(ConnectorQuery(resource="issues"))


@respx.mock
async def test_query_events_missing_project(connector: SentryConnector) -> None:
    with pytest.raises(ValueError, match="Sentry events query requires 'project'"):
        await connector.query(ConnectorQuery(resource="events"))


@respx.mock
async def test_query_issues_with_cursor(connector: SentryConnector) -> None:
    issues = [{"id": "1", "title": "After cursor"}]
    respx.get(
        f"{_BASE}/projects/{ORG}/project-alpha/issues/",
        params={"cursor": "next-page-token"},
    ).mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={"project": "project-alpha"},
            cursor="next-page-token",
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_projects_with_cursor(connector: SentryConnector) -> None:
    projects = [{"id": "p3", "slug": "project-gamma"}]
    respx.get(f"{_BASE}/projects/", params={"cursor": "next-page"}).mock(
        return_value=httpx.Response(200, json=projects)
    )
    result = await connector.query(
        ConnectorQuery(resource="projects", cursor="next-page")
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_http_401(connector: SentryConnector) -> None:
    respx.get(f"{_BASE}/projects/{ORG}/project-alpha/issues/").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(
            ConnectorQuery(resource="issues", filters={"project": "project-alpha"})
        )


@respx.mock
async def test_query_http_500(connector: SentryConnector) -> None:
    respx.get(f"{_BASE}/projects/{ORG}/project-alpha/issues/").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(
            ConnectorQuery(resource="issues", filters={"project": "project-alpha"})
        )
