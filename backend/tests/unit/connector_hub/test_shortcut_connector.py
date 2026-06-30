"""Unit tests for ShortcutConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.shortcut import ShortcutConnector

TOKEN = "shortcut_token"
_BASE = "https://api.app.shortcut.com/api/v3"


@pytest.fixture()
def connector():
    return ShortcutConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/member").mock(
        return_value=httpx.Response(200, json={"id": "u1", "mention_name": "alice"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "alice"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/member").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_fallback_name(connector):
    respx.get(f"{_BASE}/member").mock(
        return_value=httpx.Response(200, json={"id": "u1", "profile": {"name": "Alice Smith"}}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice Smith"


@respx.mock
async def test_health_check_exception(connector):
    """Non-HTTP exception (e.g. network error) returns a HealthResult with ok=False."""
    respx.get(f"{_BASE}/member").mock(side_effect=httpx.ConnectError("DNS failure"))
    result = await connector.health_check()
    assert result.ok is False
    assert "DNS failure" in result.detail


# ---------------------------------------------------------------------------
# query — list resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_stories(connector):
    stories = [
        {"id": 1, "name": "Story One", "story_type": "feature"},
        {"id": 2, "name": "Story Two", "story_type": "bug"},
    ]
    respx.get(f"{_BASE}/stories").mock(return_value=httpx.Response(200, json=stories))
    result = await connector.query(ConnectorQuery(resource="stories"))
    assert result.total == 2
    assert result.records[0]["name"] == "Story One"


@respx.mock
async def test_query_stories_with_filters(connector):
    stories = [{"id": 1, "name": "Filtered Story"}]
    respx.get(f"{_BASE}/stories").mock(return_value=httpx.Response(200, json=stories))
    result = await connector.query(
        ConnectorQuery(resource="stories", filters={"project_id": "42", "workflow_state_id": "5", "owner_id": "99"})
    )
    assert result.total == 1


@respx.mock
async def test_query_stories_with_limit(connector):
    stories = [{"id": 1, "name": "Limited Story"}]
    respx.get(f"{_BASE}/stories").mock(return_value=httpx.Response(200, json=stories))
    result = await connector.query(ConnectorQuery(resource="stories", limit=1))
    assert result.total == 1


@respx.mock
async def test_query_single_story(connector):
    story = {"id": 1, "name": "Single Story", "description": "A test story"}
    respx.get(f"{_BASE}/stories/1").mock(return_value=httpx.Response(200, json=story))
    result = await connector.query(ConnectorQuery(resource="story", filters={"story_id": "1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Single Story"


async def test_query_single_story_missing_id(connector):
    with pytest.raises(ValueError, match="'story_id' filter"):
        await connector.query(ConnectorQuery(resource="story"))


@respx.mock
async def test_query_projects(connector):
    projects = [
        {"id": 1, "name": "Project Alpha"},
        {"id": 2, "name": "Project Beta"},
    ]
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.total == 2


@respx.mock
async def test_query_projects_with_suspended_filter(connector):
    projects = [{"id": 3, "name": "Suspended Project"}]
    respx.get(f"{_BASE}/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects", filters={"suspended": True}))
    assert result.total == 1


@respx.mock
async def test_query_single_project(connector):
    project = {"id": 1, "name": "Single Project"}
    respx.get(f"{_BASE}/projects/1").mock(return_value=httpx.Response(200, json=project))
    result = await connector.query(ConnectorQuery(resource="project", filters={"project_id": "1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Single Project"


async def test_query_single_project_missing_id(connector):
    with pytest.raises(ValueError, match="'project_id' filter"):
        await connector.query(ConnectorQuery(resource="project"))


@respx.mock
async def test_query_epics(connector):
    epics = [
        {"id": 1, "name": "Epic One"},
        {"id": 2, "name": "Epic Two"},
    ]
    respx.get(f"{_BASE}/epics").mock(return_value=httpx.Response(200, json=epics))
    result = await connector.query(ConnectorQuery(resource="epics"))
    assert result.total == 2


@respx.mock
async def test_query_epics_with_suspended_filter(connector):
    epics = [{"id": 1, "name": "Suspended Epic"}]
    respx.get(f"{_BASE}/epics").mock(return_value=httpx.Response(200, json=epics))
    result = await connector.query(ConnectorQuery(resource="epics", filters={"suspended": True}))
    assert result.total == 1


@respx.mock
async def test_query_single_epic(connector):
    epic = {"id": 1, "name": "Single Epic"}
    respx.get(f"{_BASE}/epics/1").mock(return_value=httpx.Response(200, json=epic))
    result = await connector.query(ConnectorQuery(resource="epic", filters={"epic_id": "1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Single Epic"


async def test_query_single_epic_missing_id(connector):
    with pytest.raises(ValueError, match="'epic_id' filter"):
        await connector.query(ConnectorQuery(resource="epic"))


@respx.mock
async def test_query_workflows(connector):
    workflows = [
        {"id": 1, "name": "Default Workflow"},
        {"id": 2, "name": "Custom Workflow"},
    ]
    respx.get(f"{_BASE}/workflows").mock(return_value=httpx.Response(200, json=workflows))
    result = await connector.query(ConnectorQuery(resource="workflows"))
    assert result.total == 2


@respx.mock
async def test_query_members(connector):
    members = [
        {"id": "u1", "mention_name": "alice"},
        {"id": "u2", "mention_name": "bob"},
    ]
    respx.get(f"{_BASE}/members").mock(return_value=httpx.Response(200, json=members))
    result = await connector.query(ConnectorQuery(resource="members"))
    assert result.total == 2


@respx.mock
async def test_query_teams(connector):
    teams = [
        {"id": "t1", "name": "Engineering"},
        {"id": "t2", "name": "Design"},
    ]
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json=teams))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert result.total == 2


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unknown_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Shortcut query resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_story(connector):
    created = {"id": 1, "name": "New Story", "story_type": "feature", "app_url": "https://shortcut.com/story/1"}
    respx.post(f"{_BASE}/stories").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="story",
            data={"name": "New Story", "project_id": 42, "story_type": "feature"},
        )
    )
    assert result["id"] == 1
    assert result["name"] == "New Story"


@respx.mock
async def test_write_update_story(connector):
    updated = {"id": 1, "name": "Updated Name", "description": "Updated desc"}
    respx.put(f"{_BASE}/stories/1").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(
            resource="story_update",
            data={"id": "1", "name": "Updated Name"},
        )
    )
    assert result["name"] == "Updated Name"


async def test_write_update_story_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in story_update"):
        await connector.write(
            ConnectorPayload(resource="story_update", data={"name": "Orphan"})
        )


@respx.mock
async def test_write_story_comment(connector):
    comment = {"id": "c1", "text": "Nice work!", "author_id": "u1"}
    respx.post(f"{_BASE}/stories/1/comments").mock(return_value=httpx.Response(201, json=comment))
    result = await connector.write(
        ConnectorPayload(
            resource="story_comment",
            data={"story_id": "1", "text": "Nice work!"},
        )
    )
    assert result["text"] == "Nice work!"


async def test_write_story_comment_missing_fields(connector):
    with pytest.raises(ValueError, match="story_comment requires"):
        await connector.write(
            ConnectorPayload(resource="story_comment", data={"story_id": "1"})
        )

    with pytest.raises(ValueError, match="story_comment requires"):
        await connector.write(
            ConnectorPayload(resource="story_comment", data={"text": "Orphan"})
        )


@respx.mock
async def test_write_story_comment_with_optional_fields(connector):
    comment = {"id": "c1", "text": "With author", "author_id": "u1"}
    respx.post(f"{_BASE}/stories/1/comments").mock(return_value=httpx.Response(201, json=comment))
    result = await connector.write(
        ConnectorPayload(
            resource="story_comment",
            data={"story_id": "1", "text": "With author", "author_id": "u1", "external_id": "ext-1"},
        )
    )
    assert result["text"] == "With author"


@respx.mock
async def test_write_create_epic(connector):
    created = {"id": 1, "name": "New Epic", "app_url": "https://shortcut.com/epic/1"}
    respx.post(f"{_BASE}/epics").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="epic",
            data={"name": "New Epic", "description": "A new epic"},
        )
    )
    assert result["id"] == 1


async def test_write_unknown_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Shortcut write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# connector type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SHORTCUT


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_stories_http_error(connector):
    respx.get(f"{_BASE}/stories").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="stories"))


@respx.mock
async def test_write_create_story_http_error(connector):
    respx.post(f"{_BASE}/stories").mock(return_value=httpx.Response(400, text="Bad Request"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(resource="story", data={"name": "Fail Story"})
        )
