"""Unit tests for BitbucketConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors._safe_page import safe_paging_total as _paging_total
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.bitbucket import BitbucketConnector

TOKEN = "bitbucket_test_token"
_API = "https://api.bitbucket.org/2.0"


@pytest.fixture
def connector():
    return BitbucketConnector(token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/user").mock(
        return_value=httpx.Response(200, json={"username": "myuser", "display_name": "My User"})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_repos(connector):
    body = {"values": [{"uuid": "{1}", "name": "repo-a"}, {"uuid": "{2}", "name": "repo-b"}], "size": 2}
    respx.get(f"{_API}/repositories/myteam").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="repos", filters={"workspace": "myteam"}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "repo-a"
    assert result.total == 2


@respx.mock
async def test_query_file(connector):
    respx.get(f"{_API}/repositories/myteam/myrepo/src/main/README.md", headers={"Accept": "*/*"}).mock(
        return_value=httpx.Response(200, text="# Hello")
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"workspace": "myteam", "repo": "myrepo", "path": "README.md", "ref": "main"},
        )
    )
    assert result.records[0]["content"] == "# Hello"
    assert result.records[0]["path"] == "README.md"


@respx.mock
async def test_query_pulls(connector):
    body = {"values": [{"id": 42, "title": "Fix bug", "state": "OPEN"}], "size": 1}
    respx.get(f"{_API}/repositories/myteam/myrepo/pullrequests").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="pulls", filters={"workspace": "myteam", "repo": "myrepo"}))
    assert result.records[0]["id"] == 42


@respx.mock
async def test_query_issues(connector):
    body = {"values": [{"id": 7, "title": "Bug report", "state": "new"}], "size": 1}
    respx.get(f"{_API}/repositories/myteam/myrepo/issues").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"workspace": "myteam", "repo": "myrepo"}))
    assert result.records[0]["id"] == 7


@respx.mock
async def test_query_issues_with_state(connector):
    body = {"values": [], "size": 0}
    respx.get(f"{_API}/repositories/myteam/myrepo/issues").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={"workspace": "myteam", "repo": "myrepo", "state": "resolved"},
        )
    )
    assert not result.records


@respx.mock
async def test_write_file(connector):
    response_body = {"type": "commit", "hash": "abc123"}
    respx.post(f"{_API}/repositories/myteam/myrepo/src").mock(return_value=httpx.Response(201, json=response_body))
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "workspace": "myteam",
                "repo": "myrepo",
                "path": "src/main.py",
                "content": "print('hello')",
                "message": "Add main.py",
            },
        )
    )
    assert result["hash"] == "abc123"


@respx.mock
async def test_write_pull(connector):
    pr_response = {"id": 99, "title": "Add feature", "state": "OPEN"}
    respx.post(f"{_API}/repositories/myteam/myrepo/pullrequests").mock(
        return_value=httpx.Response(201, json=pr_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "workspace": "myteam",
                "repo": "myrepo",
                "title": "Add feature",
                "source_branch": "feature-branch",
                "target_branch": "main",
                "description": "Implements the feature",
            },
        )
    )
    assert result["id"] == 99


@respx.mock
async def test_write_pull_without_target_and_description(connector):
    pr_response = {"id": 100, "title": "Quick fix"}
    respx.post(f"{_API}/repositories/myteam/myrepo/pullrequests").mock(
        return_value=httpx.Response(201, json=pr_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "workspace": "myteam",
                "repo": "myrepo",
                "title": "Quick fix",
                "source_branch": "hotfix",
            },
        )
    )
    assert result["id"] == 100


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Bitbucket resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Bitbucket write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.BITBUCKET


@respx.mock
async def test_query_repos_corrupt_total(connector):
    """A non-finite ``size`` must not poison the reported total."""
    respx.get(f"{_API}/repositories/myteam").mock(
        return_value=httpx.Response(200, content=b'{"values": [{"uuid": "{1}"}], "size": 1e999}')
    )
    result = await connector.query(ConnectorQuery(resource="repos", filters={"workspace": "myteam"}))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_repos_corrupt_body_no_crash(connector):
    """A corrupt/hostile response returning a non-dict body must not crash the
    connector — it falls back to an empty page with no total."""
    respx.get(f"{_API}/repositories/myteam").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="repos", filters={"workspace": "myteam"}))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_repos_non_list_values_no_crash(connector):
    """A corrupt body placing a non-list in ``values`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_API}/repositories/myteam").mock(
        return_value=httpx.Response(200, json={"values": "not-a-list", "size": 2})
    )
    result = await connector.query(ConnectorQuery(resource="repos", filters={"workspace": "myteam"}))
    assert not result.records
    assert result.total == 2


@respx.mock
async def test_health_check_corrupt_body_no_crash(connector):
    """A corrupt/hostile user response with a non-dict body must not crash
    health_check — it reports success with an empty username."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.health_check()
    assert result.ok is True
    assert not result.detail


@respx.mock
async def test_query_issues_corrupt_body_no_crash(connector):
    respx.get(f"{_API}/repositories/myteam/myrepo/issues").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"workspace": "myteam", "repo": "myrepo"}))
    assert not result.records
    assert result.total is None


def test_paging_total() -> None:
    assert _paging_total({"size": 25}, "size") == 25
    assert _paging_total({"size": "25"}, "size") == 25
    assert _paging_total({"size": 1e999}, "size") == 0
    assert _paging_total({"size": float("nan")}, "size") == 0
    assert _paging_total({"size": True}, "size") == 0
    assert _paging_total({"size": "garbage"}, "size") == 0
    assert _paging_total({}, "size") is None
    assert _paging_total(["garbage"], "size") is None


def test_app_password_auth():
    c = BitbucketConnector(username="u", app_password="p")
    assert "Basic" in c._headers()["Authorization"]


def test_auth_missing():
    with pytest.raises(ValueError, match="Provide either token"):
        BitbucketConnector()
