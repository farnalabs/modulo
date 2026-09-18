"""Unit tests for BitbucketConnector — HTTP responses are mocked via httpx + respx."""

import base64

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.bitbucket import BitbucketConnector

TOKEN = "bb_test_token"
_BASE = "https://api.bitbucket.org/2.0"


@pytest.fixture
def connector():
    return BitbucketConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# constructor / auth
# ---------------------------------------------------------------------------


def test_token_auth_header():
    c = BitbucketConnector(token=TOKEN)
    assert c._auth_header["Authorization"] == f"Bearer {TOKEN}"


def test_basic_auth_header():
    c = BitbucketConnector(username="alice", app_password="secret")
    encoded = base64.b64encode(b"alice:secret").decode()
    assert c._auth_header["Authorization"] == f"Basic {encoded}"


def test_no_credentials_raises():
    with pytest.raises(ValueError, match="Provide either token"):
        BitbucketConnector()


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.BITBUCKET


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/user").mock(return_value=httpx.Response(200, json={"username": "alice"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "alice"


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/user").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/user").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — repos
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos(connector):
    body = {"values": [{"slug": "myrepo"}], "size": 1}
    respx.get(f"{_BASE}/repositories/myteam").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="repos", filters={"workspace": "myteam"}))
    assert result.total == 1
    assert result.records[0]["slug"] == "myrepo"


# ---------------------------------------------------------------------------
# query — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_file(connector):
    respx.get(f"{_BASE}/repositories/myteam/myrepo/src/main/README.md").mock(
        return_value=httpx.Response(200, text="# README"),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"workspace": "myteam", "repo": "myrepo", "path": "README.md"},
        ),
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "# README"
    assert result.records[0]["ref"] == "main"


async def test_query_file_missing_workspace(connector):
    with pytest.raises(ValueError, match="'workspace' filter"):
        await connector.query(ConnectorQuery(resource="file", filters={"repo": "myrepo", "path": "x"}))


async def test_query_file_missing_repo(connector):
    with pytest.raises(ValueError, match="'repo' filter"):
        await connector.query(ConnectorQuery(resource="file", filters={"workspace": "myteam", "path": "x"}))


async def test_query_file_missing_path(connector):
    with pytest.raises(ValueError, match="'path' filter"):
        await connector.query(ConnectorQuery(resource="file", filters={"workspace": "myteam", "repo": "r"}))


# ---------------------------------------------------------------------------
# query — pulls
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pulls(connector):
    body = {"values": [{"id": 1, "title": "Fix"}], "size": 1}
    respx.get(f"{_BASE}/repositories/myteam/myrepo/pullrequests").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(resource="pulls", filters={"workspace": "myteam", "repo": "myrepo", "state": "OPEN"}),
    )
    assert result.total == 1
    assert result.records[0]["id"] == 1


async def test_query_pulls_missing_workspace(connector):
    with pytest.raises(ValueError, match="'workspace' filter"):
        await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "myrepo"}))


async def test_query_pulls_missing_repo(connector):
    with pytest.raises(ValueError, match="'repo' filter"):
        await connector.query(ConnectorQuery(resource="pulls", filters={"workspace": "myteam"}))


# ---------------------------------------------------------------------------
# query — issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    body = {"values": [{"id": 5, "title": "Bug"}], "size": 1}
    respx.get(f"{_BASE}/repositories/myteam/myrepo/issues").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"workspace": "myteam", "repo": "myrepo", "state": "new"}),
    )
    assert result.total == 1


async def test_query_issues_missing_workspace(connector):
    with pytest.raises(ValueError, match="'workspace' filter"):
        await connector.query(ConnectorQuery(resource="issues", filters={"repo": "myrepo"}))


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Bitbucket resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_file(connector):
    created = {"hash": "abc123"}
    respx.post(f"{_BASE}/repositories/myteam/myrepo/src").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={"workspace": "myteam", "repo": "myrepo", "path": "README.md", "content": "# README"},
        ),
    )
    assert result["hash"] == "abc123"


# ---------------------------------------------------------------------------
# write — pull
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_pull(connector):
    created = {"id": 10, "title": "New PR"}
    respx.post(f"{_BASE}/repositories/myteam/myrepo/pullrequests").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "workspace": "myteam",
                "repo": "myrepo",
                "title": "New PR",
                "source_branch": "feature",
                "target_branch": "main",
            },
        ),
    )
    assert result["id"] == 10


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Bitbucket write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/repositories/myteam").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos", filters={"workspace": "myteam"}))
