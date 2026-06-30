"""Unit tests for CodeClimateConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.codeclimate import CodeClimateConnector

TOKEN = "cc_test_token"
_BASE = "https://api.codeclimate.com/v1"


@pytest.fixture()
def connector() -> CodeClimateConnector:
    return CodeClimateConnector(token=TOKEN)


def test_connector_type(connector: CodeClimateConnector) -> None:
    assert connector.connector_type == ConnectorType.CODECLIMATE


@respx.mock
async def test_health_check_ok(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Code Climate API token validated"


@respx.mock
async def test_health_check_invalid_token(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos", params={"limit": 1}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_network_error(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos", params={"limit": 1}).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_http_error(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos", params={"limit": 1}).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "HTTP 500" in result.detail


@respx.mock
async def test_query_repos(connector: CodeClimateConnector) -> None:
    repos = {
        "data": [
            {"id": "1", "attributes": {"github_slug": "my-org/repo-a"}},
            {"id": "2", "attributes": {"github_slug": "my-org/repo-b"}},
        ]
    }
    respx.get(f"{_BASE}/repos").mock(return_value=httpx.Response(200, json=repos))
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 2
    assert result.records[0]["attributes"]["github_slug"] == "my-org/repo-a"


@respx.mock
async def test_query_repos_with_github_slug(connector: CodeClimateConnector) -> None:
    repos = {
        "data": [
            {"id": "1", "attributes": {"github_slug": "my-org/my-repo"}},
        ]
    }
    respx.get(
        f"{_BASE}/repos",
        params={"github_slug": "my-org/my-repo"},
    ).mock(return_value=httpx.Response(200, json=repos))
    result = await connector.query(
        ConnectorQuery(resource="repos", filters={"github_slug": "my-org/my-repo"})
    )
    assert len(result.records) == 1
    assert result.records[0]["attributes"]["github_slug"] == "my-org/my-repo"


@respx.mock
async def test_query_repos_with_limit(connector: CodeClimateConnector) -> None:
    repos = {"data": [{"id": "1", "attributes": {"github_slug": "a"}}]}
    respx.get(f"{_BASE}/repos", params={"limit": 5}).mock(
        return_value=httpx.Response(200, json=repos)
    )
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert len(result.records) == 1
    assert result.total == 1


@respx.mock
async def test_query_repo(connector: CodeClimateConnector) -> None:
    repo = {"data": {"id": "r1", "attributes": {"github_slug": "my-org/my-repo"}}}
    respx.get(f"{_BASE}/repos/r1").mock(return_value=httpx.Response(200, json=repo))
    result = await connector.query(
        ConnectorQuery(resource="repo", filters={"id": "r1"})
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "r1"


@respx.mock
async def test_query_repo_missing_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate repo query requires 'id'"):
        await connector.query(ConnectorQuery(resource="repo"))


@respx.mock
async def test_query_snapshots(connector: CodeClimateConnector) -> None:
    snapshots = {
        "data": [
            {"id": "ss1", "attributes": {"branch": "main"}},
        ]
    }
    respx.get(f"{_BASE}/repos/r1/snapshots").mock(
        return_value=httpx.Response(200, json=snapshots)
    )
    result = await connector.query(
        ConnectorQuery(resource="snapshots", filters={"repo_id": "r1"})
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "ss1"


@respx.mock
async def test_query_snapshots_missing_repo_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate snapshots query requires 'repo_id'"):
        await connector.query(ConnectorQuery(resource="snapshots"))


@respx.mock
async def test_query_snapshot(connector: CodeClimateConnector) -> None:
    snapshot = {"data": {"id": "ss1", "attributes": {"branch": "main"}}}
    respx.get(f"{_BASE}/repos/r1/snapshots/ss1").mock(
        return_value=httpx.Response(200, json=snapshot)
    )
    result = await connector.query(
        ConnectorQuery(resource="snapshot", filters={"repo_id": "r1", "id": "ss1"})
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "ss1"


@respx.mock
async def test_query_snapshot_missing_repo_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate snapshot query requires 'repo_id'"):
        await connector.query(
            ConnectorQuery(resource="snapshot", filters={"id": "ss1"})
        )


@respx.mock
async def test_query_snapshot_missing_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate snapshot query requires 'id'"):
        await connector.query(
            ConnectorQuery(resource="snapshot", filters={"repo_id": "r1"})
        )


@respx.mock
async def test_query_test_reports(connector: CodeClimateConnector) -> None:
    reports = {
        "data": [
            {"id": "tr1", "attributes": {"branch": "main", "exit_code": 0}},
        ]
    }
    respx.get(f"{_BASE}/repos/r1/test_reports").mock(
        return_value=httpx.Response(200, json=reports)
    )
    result = await connector.query(
        ConnectorQuery(resource="test_reports", filters={"repo_id": "r1"})
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "tr1"


@respx.mock
async def test_query_test_reports_missing_repo_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_reports query requires 'repo_id'"):
        await connector.query(ConnectorQuery(resource="test_reports"))


@respx.mock
async def test_query_test_report(connector: CodeClimateConnector) -> None:
    report = {"data": {"id": "tr1", "attributes": {"branch": "main", "exit_code": 0}}}
    respx.get(f"{_BASE}/repos/r1/test_reports/tr1").mock(
        return_value=httpx.Response(200, json=report)
    )
    result = await connector.query(
        ConnectorQuery(resource="test_report", filters={"repo_id": "r1", "id": "tr1"})
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "tr1"


@respx.mock
async def test_query_test_report_missing_repo_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_report query requires 'repo_id'"):
        await connector.query(
            ConnectorQuery(resource="test_report", filters={"id": "tr1"})
        )


@respx.mock
async def test_query_test_report_missing_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_report query requires 'id'"):
        await connector.query(
            ConnectorQuery(resource="test_report", filters={"repo_id": "r1"})
        )


@respx.mock
async def test_write_test_report(connector: CodeClimateConnector) -> None:
    expected_body = {
        "data": {
            "type": "test_reports",
            "attributes": {
                "duration": 1200,
                "exit_code": 0,
                "branch": "main",
                "commit_sha": "abc123",
            },
        }
    }
    respx.post(f"{_BASE}/repos/r1/test_reports", json=expected_body).mock(
        return_value=httpx.Response(201, json={"data": {"id": "tr1"}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="test_report",
            data={
                "repo_id": "r1",
                "duration": 1200,
                "exit_code": 0,
                "branch": "main",
                "commit_sha": "abc123",
            },
        )
    )
    assert result["data"]["id"] == "tr1"


@respx.mock
async def test_write_test_report_with_files(connector: CodeClimateConnector) -> None:
    expected_body = {
        "data": {
            "type": "test_reports",
            "attributes": {
                "duration": 500,
                "exit_code": 1,
                "branch": "develop",
                "commit_sha": "def456",
                "files": [{"path": "tests/test_a.py", "coverage": 90}],
            },
        }
    }
    respx.post(f"{_BASE}/repos/r1/test_reports", json=expected_body).mock(
        return_value=httpx.Response(201, json={"data": {"id": "tr2"}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="test_report",
            data={
                "repo_id": "r1",
                "duration": 500,
                "exit_code": 1,
                "branch": "develop",
                "commit_sha": "def456",
                "files": [{"path": "tests/test_a.py", "coverage": 90}],
            },
        )
    )
    assert result["data"]["id"] == "tr2"


@respx.mock
async def test_write_test_report_missing_repo_id(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_report write requires 'repo_id'"):
        await connector.write(
            ConnectorPayload(
                resource="test_report",
                data={"duration": 1200, "exit_code": 0, "commit_sha": "abc123"},
            )
        )


@respx.mock
async def test_write_test_report_missing_duration(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_report write requires 'duration'"):
        await connector.write(
            ConnectorPayload(
                resource="test_report",
                data={"repo_id": "r1", "exit_code": 0, "commit_sha": "abc123"},
            )
        )


@respx.mock
async def test_write_test_report_missing_exit_code(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_report write requires 'exit_code'"):
        await connector.write(
            ConnectorPayload(
                resource="test_report",
                data={"repo_id": "r1", "duration": 1200, "commit_sha": "abc123"},
            )
        )


@respx.mock
async def test_write_test_report_missing_commit_sha(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Code Climate test_report write requires 'commit_sha'"):
        await connector.write(
            ConnectorPayload(
                resource="test_report",
                data={"repo_id": "r1", "duration": 1200, "exit_code": 0},
            )
        )


async def test_query_invalid_resource(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Code Climate resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector: CodeClimateConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Code Climate write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_repos_with_http_401(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_with_http_500(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_with_cursor(connector: CodeClimateConnector) -> None:
    repos = {"data": [{"id": "1", "attributes": {"github_slug": "a"}}]}
    next_url = f"{_BASE}/repos?cursor=abc123"
    resp = httpx.Response(
        200,
        json=repos,
        headers={"link": f'<{next_url}>; rel="next"'},
    )
    respx.get(f"{_BASE}/repos").mock(return_value=resp)
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1


@respx.mock
async def test_query_repo_empty_result(connector: CodeClimateConnector) -> None:
    respx.get(f"{_BASE}/repos/nonexistent").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    result = await connector.query(
        ConnectorQuery(resource="repo", filters={"id": "nonexistent"})
    )
    assert len(result.records) == 0


@respx.mock
async def test_snapshots_with_limit(connector: CodeClimateConnector) -> None:
    snapshots = {"data": [{"id": "ss1"}, {"id": "ss2"}]}
    respx.get(f"{_BASE}/repos/r1/snapshots", params={"limit": 2}).mock(
        return_value=httpx.Response(200, json=snapshots)
    )
    result = await connector.query(
        ConnectorQuery(resource="snapshots", filters={"repo_id": "r1"}, limit=2)
    )
    assert len(result.records) == 2


@respx.mock
async def test_test_reports_with_limit(connector: CodeClimateConnector) -> None:
    reports = {"data": [{"id": "tr1"}]}
    respx.get(f"{_BASE}/repos/r1/test_reports", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json=reports)
    )
    result = await connector.query(
        ConnectorQuery(resource="test_reports", filters={"repo_id": "r1"}, limit=1)
    )
    assert len(result.records) == 1


def test_client_auth_header(connector: CodeClimateConnector) -> None:
    client = connector._client()
    assert client.headers["Authorization"] == f"Token token={TOKEN}"
