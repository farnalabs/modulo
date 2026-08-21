"""Unit tests for AsanaConnector list-parsing hardening using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.asana import AsanaConnector
from modulo.connectors.base import ConnectorQuery, ConnectorType

PAT = "asana_pat_123"
_BASE = "https://app.asana.com/api/1.0"


@pytest.fixture
def connector() -> AsanaConnector:
    return AsanaConnector(personal_access_token=PAT)


def test_connector_type(connector: AsanaConnector) -> None:
    assert connector.connector_type == ConnectorType.ASANA


# ---------------------------------------------------------------------------
# Corrupt list payload hardening
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects_corrupt_body_no_crash(connector: AsanaConnector) -> None:
    """A non-dict body from the projects endpoint must degrade to an empty
    page instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_projects_non_list_data_value_no_crash(connector: AsanaConnector) -> None:
    """A corrupt body placing a non-list in ``data`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json={"data": "not-a-list"}))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_tasks_corrupt_body_no_crash(connector: AsanaConnector) -> None:
    """A non-dict body from the tasks endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/tasks").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="tasks", filters={"workspace": "w1"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_sections_corrupt_body_no_crash(connector: AsanaConnector) -> None:
    """A non-dict body from the sections endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/projects/p1/sections").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="sections", filters={"project_id": "p1"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_workspaces_corrupt_body_no_crash(connector: AsanaConnector) -> None:
    """A non-dict body from the workspaces endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/workspaces").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="workspaces"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_users_corrupt_body_no_crash(connector: AsanaConnector) -> None:
    """A non-dict body from the users endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_single_project_corrupt_body_no_crash(connector: AsanaConnector) -> None:
    """A non-dict body from the single-project endpoint must degrade to an
    empty result instead of crashing with AttributeError."""
    respx.get(f"{_BASE}/projects/p1").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="project", filters={"project_id": "p1"}))
    assert not result.records
