"""Unit tests for SentryConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.sentry import SentryConnector

TOKEN = "sentry_test_token"
_ORG = "myorg"
_PROJECT = "web"
_BASE = "https://sentry.io/api/0"


@pytest.fixture
def connector():
    return SentryConnector(token=TOKEN, organization=_ORG)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SENTRY


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(200, json={"version": "25.1.0"}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Sentry auth token" in result.detail


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


# ---------------------------------------------------------------------------
# query — issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    body = [{"id": "1", "title": "Null pointer"}]
    respx.get(f"{_BASE}/projects/{_ORG}/{_PROJECT}/issues/").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": _PROJECT}))
    assert result.total == 1
    assert result.records[0]["id"] == "1"


async def test_query_issues_missing_project(connector):
    query = ConnectorQuery(resource="issues")
    with pytest.raises(ValueError, match="'project' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — events
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_events(connector):
    body = [{"id": "e1", "eventID": "abc"}]
    respx.get(f"{_BASE}/projects/{_ORG}/{_PROJECT}/events/").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="events", filters={"project": _PROJECT}))
    assert result.total == 1


async def test_query_events_missing_project(connector):
    query = ConnectorQuery(resource="events")
    with pytest.raises(ValueError, match="'project' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — projects
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    body = [{"id": "p1", "slug": _PROJECT}]
    respx.get(f"{_BASE}/projects/").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.total == 1
    assert result.records[0]["slug"] == _PROJECT


# ---------------------------------------------------------------------------
# query — releases
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_releases(connector):
    body = [{"version": "1.0.0"}]
    respx.get(f"{_BASE}/organizations/{_ORG}/releases/").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="releases"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — teams
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_teams(connector):
    body = [{"id": "t1", "name": "Core"}]
    respx.get(f"{_BASE}/organizations/{_ORG}/teams/").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — issue_events
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue_events(connector):
    body = [{"id": "ie1"}]
    respx.get(f"{_BASE}/issues/1/events/").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="issue_events", filters={"issue_id": "1"}))
    assert result.total == 1


async def test_query_issue_events_missing_issue_id(connector):
    query = ConnectorQuery(resource="issue_events")
    with pytest.raises(ValueError, match="'issue_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    query = ConnectorQuery(resource="invalid")
    with pytest.raises(ValueError, match="Unsupported Sentry resource"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# write — issue_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_status(connector):
    updated = {"id": "1", "status": "resolved"}
    respx.put(f"{_BASE}/issues/1/").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(resource="issue_status", data={"issue_id": "1", "status": "resolved"}),
    )
    assert result["status"] == "resolved"


async def test_write_issue_status_missing_issue_id(connector):
    payload = ConnectorPayload(resource="issue_status", data={"status": "resolved"})
    with pytest.raises(ValueError, match="'issue_id' in data"):
        await connector.write(payload)


async def test_write_issue_status_missing_status(connector):
    with pytest.raises(ValueError, match="'status' in data"):
        await connector.write(ConnectorPayload(resource="issue_status", data={"issue_id": "1"}))


# ---------------------------------------------------------------------------
# write — event_comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_event_comment(connector):
    created = {"id": "c1", "data": {"text": "me too"}}
    respx.post(f"{_BASE}/issues/1/comments/").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="event_comment", data={"issue_id": "1", "text": "me too"}),
    )
    assert result["id"] == "c1"


async def test_write_event_comment_missing_text(connector):
    with pytest.raises(ValueError, match="'text' in data"):
        await connector.write(ConnectorPayload(resource="event_comment", data={"issue_id": "1"}))


# ---------------------------------------------------------------------------
# write — release
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_release(connector):
    created = {"id": "r1", "version": "2.0.0"}
    respx.post(f"{_BASE}/organizations/{_ORG}/releases/").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(ConnectorPayload(resource="release", data={"version": "2.0.0"}))
    assert result["version"] == "2.0.0"


async def test_write_release_missing_version(connector):
    with pytest.raises(ValueError, match="'version' in data"):
        await connector.write(ConnectorPayload(resource="release", data={}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Sentry write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/projects/").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="projects"))
