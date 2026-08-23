"""Unit tests for AsanaConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.asana import AsanaConnector
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType

PAT = "asana_pat_123"
_BASE = "https://app.asana.com/api/1.0"


@pytest.fixture
def connector():
    return AsanaConnector(personal_access_token=PAT)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.ASANA


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"data": {"gid": "12345", "name": "Alice Smith"}}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice Smith"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/users/me").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


# ---------------------------------------------------------------------------
# query — workspaces
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_workspaces(connector):
    workspaces = {"data": [{"gid": "w1", "name": "My Workspace"}, {"gid": "w2", "name": "Team Workspace"}]}
    respx.get(f"{_BASE}/workspaces").mock(return_value=httpx.Response(200, json=workspaces))
    result = await connector.query(ConnectorQuery(resource="workspaces"))
    assert result.total == 2
    assert result.records[0]["name"] == "My Workspace"


# ---------------------------------------------------------------------------
# query — users
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_users(connector):
    users = {"data": [{"gid": "u1", "name": "Alice"}, {"gid": "u2", "name": "Bob"}]}
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=users))
    result = await connector.query(ConnectorQuery(resource="users", filters={"workspace": "w1"}))
    assert result.total == 2
    assert result.records[0]["name"] == "Alice"


# ---------------------------------------------------------------------------
# query — projects (list)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    projects = {
        "data": [
            {"gid": "p1", "name": "Project Alpha"},
            {"gid": "p2", "name": "Project Beta"},
        ]
    }
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects", filters={"workspace": "w1"}))
    assert result.total == 2
    assert result.records[0]["name"] == "Project Alpha"


@respx.mock
async def test_query_projects_with_archived(connector):
    projects = {"data": [{"gid": "p3", "name": "Archived", "archived": True}]}
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects", filters={"workspace": "w1", "archived": True}))
    assert result.total == 1
    assert result.records[0]["archived"] is True


# ---------------------------------------------------------------------------
# query — single project
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_project(connector):
    project = {"data": {"gid": "p1", "name": "Project Alpha", "notes": "A project"}}
    respx.get(f"{_BASE}/projects/p1").mock(return_value=httpx.Response(200, json=project))
    result = await connector.query(ConnectorQuery(resource="project", filters={"project_id": "p1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Project Alpha"


async def test_query_single_project_missing_id(connector):
    query = ConnectorQuery(resource="project")
    with pytest.raises(ValueError, match="'project_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — tasks (list)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_tasks_by_project(connector):
    tasks = {
        "data": [
            {"gid": "t1", "name": "Task One"},
            {"gid": "t2", "name": "Task Two"},
        ]
    }
    respx.get(f"{_BASE}/projects/p1/tasks").mock(return_value=httpx.Response(200, json=tasks))
    result = await connector.query(ConnectorQuery(resource="tasks", filters={"project_id": "p1"}))
    assert result.total == 2
    assert result.records[0]["name"] == "Task One"


@respx.mock
async def test_query_tasks_by_workspace(connector):
    tasks = {"data": [{"gid": "t3", "name": "Workspace Task"}]}
    respx.get(f"{_BASE}/tasks").mock(return_value=httpx.Response(200, json=tasks))
    result = await connector.query(ConnectorQuery(resource="tasks", filters={"workspace": "w1"}))
    assert result.total == 1


async def test_query_tasks_missing_filter(connector):
    query = ConnectorQuery(resource="tasks")
    with pytest.raises(ValueError, match="'project_id' or 'workspace' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — single task
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_task(connector):
    task = {"data": {"gid": "t1", "name": "Single Task", "notes": "Important"}}
    respx.get(f"{_BASE}/tasks/t1").mock(return_value=httpx.Response(200, json=task))
    result = await connector.query(ConnectorQuery(resource="task", filters={"task_id": "t1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Single Task"


async def test_query_single_task_missing_id(connector):
    query = ConnectorQuery(resource="task")
    with pytest.raises(ValueError, match="'task_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — sections
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_sections(connector):
    sections = {
        "data": [
            {"gid": "s1", "name": "To Do"},
            {"gid": "s2", "name": "In Progress"},
        ]
    }
    respx.get(f"{_BASE}/projects/p1/sections").mock(return_value=httpx.Response(200, json=sections))
    result = await connector.query(ConnectorQuery(resource="sections", filters={"project_id": "p1"}))
    assert result.total == 2
    assert result.records[0]["name"] == "To Do"


async def test_query_sections_missing_project_id(connector):
    query = ConnectorQuery(resource="sections")
    with pytest.raises(ValueError, match="'project_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    query = ConnectorQuery(resource="unknown")
    with pytest.raises(ValueError, match="Unsupported Asana resource"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# write — create task
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_task(connector):
    created = {"data": {"gid": "t_new", "name": "New Task", "resource_type": "task"}}
    respx.post(f"{_BASE}/tasks").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(ConnectorPayload(resource="task", data={"name": "New Task", "projects": ["p1"]}))
    assert result["gid"] == "t_new"
    assert result["name"] == "New Task"


# ---------------------------------------------------------------------------
# write — update task
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_update_task(connector):
    updated = {"data": {"gid": "t1", "name": "Updated Name", "completed": True}}
    respx.put(f"{_BASE}/tasks/t1").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(ConnectorPayload(resource="task_update", data={"id": "t1", "name": "Updated Name"}))
    assert result["name"] == "Updated Name"


async def test_write_update_task_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(ConnectorPayload(resource="task_update", data={"name": "Orphan"}))


# ---------------------------------------------------------------------------
# write — create project
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_project(connector):
    created = {"data": {"gid": "p_new", "name": "New Project", "resource_type": "project"}}
    respx.post(f"{_BASE}/projects").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(ConnectorPayload(resource="project", data={"name": "New Project"}))
    assert result["gid"] == "p_new"
    assert result["name"] == "New Project"


# ---------------------------------------------------------------------------
# write — create section
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_section(connector):
    created = {"data": {"gid": "s_new", "name": "New Section", "resource_type": "section"}}
    respx.post(f"{_BASE}/sections").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(ConnectorPayload(resource="section", data={"project": "p1", "name": "New Section"}))
    assert result["gid"] == "s_new"
    assert result["name"] == "New Section"


async def test_write_create_section_missing_project(connector):
    with pytest.raises(ValueError, match="'project' in data"):
        await connector.write(ConnectorPayload(resource="section", data={"name": "Orphan"}))


# ---------------------------------------------------------------------------
# write — add comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_comment(connector):
    comment = {"data": {"gid": "st1", "text": "Nice work!", "resource_type": "story"}}
    respx.post(f"{_BASE}/tasks/t1/stories").mock(return_value=httpx.Response(201, json=comment))
    result = await connector.write(ConnectorPayload(resource="comment", data={"task_id": "t1", "text": "Nice work!"}))
    assert result["text"] == "Nice work!"


async def test_write_comment_missing_task_id(connector):
    with pytest.raises(ValueError, match="'task_id' in data"):
        await connector.write(ConnectorPayload(resource="comment", data={"text": "Orphan"}))


# ---------------------------------------------------------------------------
# write — unknown resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Asana write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_http_error_propagation(connector):
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="projects"))
