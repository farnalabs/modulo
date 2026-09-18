"""Unit tests for AzureReposConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType

TOKEN = "azr_test_token"
_ORG = "myorg"
_PROJECT = "myproject"
_BASE = f"https://dev.azure.com/{_ORG}"
_PROFILE = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"


@pytest.fixture
def connector():
    return AzureReposConnector(token=TOKEN, organization=_ORG)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.AZURE_REPOS


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(_PROFILE).mock(return_value=httpx.Response(200, json={"displayName": "Alice"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(_PROFILE).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(_PROFILE).mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(_PROFILE).mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — repos
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos(connector):
    body = {"value": [{"id": "r1", "name": "myrepo"}], "count": 1}
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="repos", filters={"project": _PROJECT}))
    assert result.total == 1
    assert result.records[0]["name"] == "myrepo"


# ---------------------------------------------------------------------------
# query — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_file(connector):
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/items").mock(
        return_value=httpx.Response(200, text="# README"),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"project": _PROJECT, "repo": "myrepo", "path": "README.md"},
        ),
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "# README"
    assert result.records[0]["ref"] == "main"


# ---------------------------------------------------------------------------
# query — pulls
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pulls(connector):
    body = {"value": [{"pullRequestId": 1, "title": "Fix"}], "count": 1}
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/pullrequests").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(resource="pulls", filters={"project": _PROJECT, "repo": "myrepo"}),
    )
    assert result.total == 1
    assert result.records[0]["pullRequestId"] == 1


# ---------------------------------------------------------------------------
# query — commits
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_commits(connector):
    body = {"value": [{"commitId": "abc123"}], "count": 1}
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/commits").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(resource="commits", filters={"project": _PROJECT, "repo": "myrepo"}),
    )
    assert result.total == 1
    assert result.records[0]["commitId"] == "abc123"


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Azure Repos resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_file(connector):
    refs_body = {
        "value": [
            {
                "name": "refs/heads/main",
                "objectId": "0000000000000000000000000000000000000001",
            },
        ],
    }
    push_body = {"pushId": 1, "refUpdates": [{"name": "refs/heads/main"}]}
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/refs").mock(
        return_value=httpx.Response(200, json=refs_body),
    )
    respx.post(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/pushes").mock(
        return_value=httpx.Response(200, json=push_body),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "project": _PROJECT,
                "repo": "myrepo",
                "path": "README.md",
                "content": "# README",
            },
        ),
    )
    assert result["pushId"] == 1


@respx.mock
async def test_write_file_branch_not_found(connector):
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/refs").mock(
        return_value=httpx.Response(200, json={"value": []}),
    )
    with pytest.raises(ValueError, match="not found in repo"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={
                    "project": _PROJECT,
                    "repo": "myrepo",
                    "path": "README.md",
                    "content": "x",
                },
            ),
        )


# ---------------------------------------------------------------------------
# write — pull
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_pull(connector):
    created = {"pullRequestId": 5, "title": "New PR"}
    respx.post(f"{_BASE}/{_PROJECT}/_apis/git/repositories/myrepo/pullrequests").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "project": _PROJECT,
                "repo": "myrepo",
                "title": "New PR",
                "source_branch": "feature",
                "target_branch": "main",
            },
        ),
    )
    assert result["pullRequestId"] == 5


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Azure Repos write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/{_PROJECT}/_apis/git/repositories").mock(
        return_value=httpx.Response(500, text="Internal Error"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos", filters={"project": _PROJECT}))
