"""Unit tests for CodeClimateConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.codeclimate import CodeClimateConnector

TOKEN = "cc_test_token"
_BASE = "https://api.codeclimate.com/v1"


@pytest.fixture
def connector():
    return CodeClimateConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.CODECLIMATE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(200, json={"data": []}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Code Climate auth token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# query — repos
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos(connector):
    data = [{"id": "repo-123", "type": "repos", "attributes": {"name": "my-repo"}}]
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(ConnectorQuery(resource="repos", limit=10))
    assert result.total == 1
    assert result.records[0]["id"] == "repo-123"


@respx.mock
async def test_query_repos_filtered_by_github_slug(connector):
    data = [{"id": "repo-456", "type": "repos"}]
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(
        ConnectorQuery(resource="repos", filters={"github_slug": "my-org/my-repo"}),
    )
    assert result.total == 1
    assert result.records[0]["id"] == "repo-456"


# ---------------------------------------------------------------------------
# query — repo
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repo(connector):
    data = {"id": "repo-123", "type": "repos"}
    respx.get(f"{_BASE}/repos/repo-123").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(ConnectorQuery(resource="repo", filters={"id": "repo-123"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "repo-123"


async def test_query_repo_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in filters"):
        await connector.query(ConnectorQuery(resource="repo"))


# ---------------------------------------------------------------------------
# query — snapshots
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_snapshots(connector):
    data = [{"id": "ss-456", "type": "snapshots"}]
    respx.get(f"{_BASE}/repos/repo-123/snapshots").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(ConnectorQuery(resource="snapshots", filters={"repo_id": "repo-123"}))
    assert result.total == 1


@respx.mock
async def test_query_snapshot(connector):
    data = {"id": "ss-456", "type": "snapshots"}
    respx.get(f"{_BASE}/repos/repo-123/snapshots/ss-456").mock(
        return_value=httpx.Response(200, json={"data": data}),
    )
    result = await connector.query(
        ConnectorQuery(resource="snapshot", filters={"repo_id": "repo-123", "id": "ss-456"}),
    )
    assert result.records[0]["id"] == "ss-456"


async def test_query_snapshots_missing_repo_id(connector):
    with pytest.raises(ValueError, match="'repo_id' in filters"):
        await connector.query(ConnectorQuery(resource="snapshots"))


async def test_query_snapshot_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in filters"):
        await connector.query(ConnectorQuery(resource="snapshot", filters={"repo_id": "repo-123"}))


# ---------------------------------------------------------------------------
# query — test reports
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_test_reports(connector):
    data = [{"id": "tr-789", "type": "test_reports"}]
    respx.get(f"{_BASE}/repos/repo-123/test_reports").mock(
        return_value=httpx.Response(200, json={"data": data}),
    )
    result = await connector.query(ConnectorQuery(resource="test_reports", filters={"repo_id": "repo-123"}))
    assert result.total == 1


@respx.mock
async def test_query_test_report(connector):
    data = {"id": "tr-789", "type": "test_reports"}
    respx.get(f"{_BASE}/repos/repo-123/test_reports/tr-789").mock(
        return_value=httpx.Response(200, json={"data": data}),
    )
    result = await connector.query(
        ConnectorQuery(resource="test_report", filters={"repo_id": "repo-123", "id": "tr-789"}),
    )
    assert result.records[0]["id"] == "tr-789"


async def test_query_test_reports_missing_repo_id(connector):
    with pytest.raises(ValueError, match="'repo_id' in filters"):
        await connector.query(ConnectorQuery(resource="test_reports"))


# ---------------------------------------------------------------------------
# write — test_report
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_test_report(connector):
    created = {"data": {"id": "tr-new", "type": "test_reports"}}
    respx.post(f"{_BASE}/repos/repo-123/test_reports").mock(
        return_value=httpx.Response(201, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="test_report",
            data={
                "repo_id": "repo-123",
                "duration": 1200,
                "exit_code": 0,
                "branch": "main",
                "commit_sha": "abc123",
            },
        )
    )
    assert result["data"]["id"] == "tr-new"


@respx.mock
async def test_write_test_report_with_files(connector):
    created = {"data": {"id": "tr-new", "type": "test_reports"}}
    respx.post(f"{_BASE}/repos/repo-123/test_reports").mock(
        return_value=httpx.Response(201, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="test_report",
            data={
                "repo_id": "repo-123",
                "duration": 1200,
                "exit_code": 0,
                "commit_sha": "abc123",
                "files": [{"name": "spec.js"}],
            },
        )
    )
    assert result["data"]["id"] == "tr-new"


async def test_write_test_report_missing_fields(connector):
    with pytest.raises(ValueError, match="'repo_id' in data"):
        await connector.write(ConnectorPayload(resource="test_report", data={"duration": 1}))
    with pytest.raises(ValueError, match="'duration' in data"):
        await connector.write(
            ConnectorPayload(resource="test_report", data={"repo_id": "repo-123", "exit_code": 0}),
        )
    with pytest.raises(ValueError, match="'exit_code' in data"):
        await connector.write(
            ConnectorPayload(
                resource="test_report",
                data={"repo_id": "repo-123", "duration": 1, "commit_sha": "abc"},
            ),
        )
    with pytest.raises(ValueError, match="'commit_sha' in data"):
        await connector.write(
            ConnectorPayload(
                resource="test_report",
                data={"repo_id": "repo-123", "duration": 1, "exit_code": 0},
            ),
        )


# ---------------------------------------------------------------------------
# query / write — unsupported
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Code Climate resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Code Climate write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos"))
