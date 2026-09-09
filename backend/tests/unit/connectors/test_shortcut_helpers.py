"""Unit tests for the extracted query-helper methods in ShortcutConnector."""

from __future__ import annotations

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.shortcut import ShortcutConnector

TOKEN = "shortcut_token"
_BASE = "https://api.app.shortcut.com/api/v3"


@pytest.fixture
def connector():
    return ShortcutConnector(token=TOKEN)


@respx.mock
async def test_query_list(connector):
    respx.get(f"{_BASE}/stories").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector._query_list("stories")
    assert result.records == [{"id": 1}]
    assert result.total == 1


@respx.mock
async def test_query_list_collection_stories_with_filters(connector):
    respx.get(f"{_BASE}/stories").mock(return_value=httpx.Response(200, json=[{"id": 7}]))
    q = ConnectorQuery(resource="stories", filters={"project_id": "p1", "owner_id": "u2"}, limit=5)
    result = await connector._query_list_collection(q, "stories")
    assert result.records == [{"id": 7}]
    req = respx.calls.last.request
    assert req.url.params["project_id"] == "p1"
    assert req.url.params["owner_id"] == "u2"
    assert req.url.params["limit"] == "5"


@respx.mock
async def test_query_list_collection_projects_suspended(connector):
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json=[]))
    q = ConnectorQuery(resource="projects", filters={"suspended": True})
    result = await connector._query_list_collection(q, "projects")
    assert not result.records
    assert respx.calls.last.request.url.params["suspended"] == "true"


@respx.mock
async def test_query_single_by_resource_story(connector):
    respx.get(f"{_BASE}/stories/123").mock(return_value=httpx.Response(200, json={"id": 123}))
    q = ConnectorQuery(resource="story", filters={"story_id": "123"})
    result = await connector._query_single_by_resource(q, "story")
    assert result.records == [{"id": 123}]


@respx.mock
async def test_query_single_by_resource_epic(connector):
    respx.get(f"{_BASE}/epics/9").mock(return_value=httpx.Response(200, json={"id": 9}))
    q = ConnectorQuery(resource="epic", filters={"epic_id": "9"})
    result = await connector._query_single_by_resource(q, "epic")
    assert result.records == [{"id": 9}]


async def test_query_single_by_resource_missing_id_raises(connector):
    q = ConnectorQuery(resource="story", filters={})
    with pytest.raises(ValueError, match="story_id"):
        await connector._query_single_by_resource(q, "story")


@respx.mock
async def test_query_single_by_resource_project(connector):
    respx.get(f"{_BASE}/projects/42").mock(return_value=httpx.Response(200, json={"id": 42}))
    q = ConnectorQuery(resource="project", filters={"project_id": "42"})
    result = await connector._query_single_by_resource(q, "project")
    assert result.records == [{"id": 42}]


@respx.mock
async def test_query_list_collection_epics_suspended(connector):
    respx.get(f"{_BASE}/epics").mock(return_value=httpx.Response(200, json=[{"id": 3}]))
    q = ConnectorQuery(resource="epics", filters={"suspended": False})
    result = await connector._query_list_collection(q, "epics")
    assert result.records == [{"id": 3}]
    assert respx.calls.last.request.url.params["suspended"] == "false"
