"""Unit tests for GitLabConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.gitlab import GitLabConnector

TOKEN = "glpat_test_token"
_API = "https://gitlab.com/api/v4"


@pytest.fixture()
def connector():
    return GitLabConnector(token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_missing_scopes(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(403, text="forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Missing scopes" in result.detail


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_projects(connector):
    projects = [{"id": 1, "name": "proj-a"}, {"id": 2, "name": "proj-b"}]
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "proj-a"


@respx.mock
async def test_query_file(connector):
    file_data = {
        "file_name": "README.md",
        "content": "SGVsbG8gV29ybGQ=",
    }
    respx.get(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"project": "group/project", "path": "README.md", "ref": "main"},
        )
    )
    assert result.records[0]["content"] == "Hello World"


@respx.mock
async def test_query_mrs(connector):
    mrs = [{"id": 42, "title": "Fix bug"}]
    respx.get(f"{_API}/projects/group%2Fproject/merge_requests").mock(return_value=httpx.Response(200, json=mrs))
    result = await connector.query(ConnectorQuery(resource="mrs", filters={"project": "group/project"}))
    assert result.records[0]["id"] == 42


@respx.mock
async def test_write_file(connector):
    response_body = {"file_path": "src/main.py", "branch": "main"}
    respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "project": "group/project",
                "path": "src/main.py",
                "content": "print('hello')",
                "message": "Update file",
            },
        )
    )
    assert result["file_path"] == "src/main.py"


@respx.mock
async def test_write_mr(connector):
    mr_response = {"id": 99, "web_url": "https://gitlab.com/group/project/-/merge_requests/99"}
    respx.post(f"{_API}/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=mr_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr",
            data={
                "project": "group/project",
                "title": "Add feature",
                "source_branch": "feature-branch",
                "target_branch": "main",
                "description": "Implements the feature",
            },
        )
    )
    assert result["id"] == 99


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitLab resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitLab write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.GITLAB
