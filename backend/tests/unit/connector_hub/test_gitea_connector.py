"""Unit tests for GiteaConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.gitea import GiteaConnector

TOKEN = "gitea_test_token"
_BASE = "https://gitea.example.com"
_API = f"{_BASE}/api/v1"


@pytest.fixture
def connector():
    return GiteaConnector(token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.GITEA


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"login": "alice"}))
    respx.get(f"{_API}/repos").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "alice"


@respx.mock
async def test_health_check_missing_scopes(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"login": "alice"}))
    respx.get(f"{_API}/repos").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Missing scopes" in result.detail


@respx.mock
async def test_health_check_forbidden_scope_inference(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"login": "alice"}))
    respx.get(f"{_API}/repos").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Missing scopes: read:repository" not in result.detail
    assert "write:repository" in result.detail


@respx.mock
async def test_health_check_unauthorized(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_API}/user").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — repos
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos(connector):
    repos = [{"id": 1, "full_name": "owner/repo"}]
    respx.get(f"{_API}/user/repos").mock(return_value=httpx.Response(200, json=repos))
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.total == 1
    assert result.records[0]["full_name"] == "owner/repo"


# ---------------------------------------------------------------------------
# query — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_file(connector):
    import base64

    encoded = base64.b64encode(b"# README\nHello").decode()
    body = {"name": "README.md", "encoding": "base64", "content": encoded}
    respx.get(f"{_API}/repos/owner/repo/contents/README.md").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "README.md"}),
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "# README\nHello"


# ---------------------------------------------------------------------------
# query — pulls
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pulls(connector):
    prs = [{"id": 10, "title": "Fix bug", "state": "open"}]
    respx.get(f"{_API}/repos/owner/repo/pulls").mock(return_value=httpx.Response(200, json=prs))
    result = await connector.query(
        ConnectorQuery(resource="pulls", filters={"repo": "owner/repo", "state": "open"}),
    )
    assert result.total == 1
    assert result.records[0]["title"] == "Fix bug"


# ---------------------------------------------------------------------------
# query — issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    issues = [{"id": 20, "title": "Bug report", "state": "open"}]
    respx.get(f"{_API}/repos/owner/repo/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"repo": "owner/repo", "state": "open"}),
    )
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Gitea resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_file(connector):
    created = {"content": {"name": "new.md"}}
    respx.put(f"{_API}/repos/owner/repo/contents/docs/new.md").mock(
        return_value=httpx.Response(201, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={"repo": "owner/repo", "path": "docs/new.md", "content": "base64content"},
        ),
    )
    assert result["content"]["name"] == "new.md"


@respx.mock
async def test_write_file_update_with_sha(connector):
    created = {"content": {"name": "new.md"}}
    respx.put(f"{_API}/repos/owner/repo/contents/docs/new.md").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={"repo": "owner/repo", "path": "docs/new.md", "content": "c2hh", "sha": "abc123"},
        ),
    )
    assert result["content"]["name"] == "new.md"


# ---------------------------------------------------------------------------
# write — pull
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_pull(connector):
    created = {"id": 11, "title": "Fix bug"}
    respx.post(f"{_API}/repos/owner/repo/pulls").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={"repo": "owner/repo", "title": "Fix bug", "head": "feature", "base": "main"},
        ),
    )
    assert result["id"] == 11


# ---------------------------------------------------------------------------
# write — issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue(connector):
    created = {"id": 21, "title": "Bug report"}
    respx.post(f"{_API}/repos/owner/repo/issues").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={"repo": "owner/repo", "title": "Bug report"},
        ),
    )
    assert result["id"] == 21


@respx.mock
async def test_write_issue_with_labels(connector):
    created = {"id": 22, "title": "Bug report"}
    respx.post(f"{_API}/repos/owner/repo/issues").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={"repo": "owner/repo", "title": "Bug report", "labels": ["bug"], "assignees": ["alice"]},
        ),
    )
    assert result["id"] == 22


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Gitea write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_API}/user/repos").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos"))
