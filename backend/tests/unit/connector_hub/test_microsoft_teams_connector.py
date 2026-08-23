"""Unit tests for MicrosoftTeamsConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector

TOKEN = "ms_test_token"
_BASE = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def connector():
    return MicrosoftTeamsConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.MICROSOFT_TEAMS


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json={"value": []}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Microsoft Graph API token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/users").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False


# ---------------------------------------------------------------------------
# query — teams / team
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_teams(connector):
    body = {"value": [{"id": "t1", "displayName": "Platform"}]}
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert result.total == 1
    assert result.records[0]["id"] == "t1"


@respx.mock
async def test_query_teams_pagination(connector):
    body = {
        "value": [{"id": "t1"}],
        "@odata.nextLink": f"{_BASE}/teams?$skiptoken=abc123",
    }
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert result.next_cursor == "abc123"


@respx.mock
async def test_query_team(connector):
    body = {"id": "t1", "displayName": "Platform"}
    respx.get(f"{_BASE}/teams/t1").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="team", filters={"team_id": "t1"}))
    assert len(result.records) == 1


async def test_query_team_missing_team_id(connector):
    query = ConnectorQuery(resource="team")
    with pytest.raises(ValueError, match="'team_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — channels / channel
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_channels(connector):
    body = {"value": [{"id": "c1", "displayName": "general"}]}
    respx.get(f"{_BASE}/teams/t1/channels").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="channels", filters={"team_id": "t1"}))
    assert result.total == 1


async def test_query_channels_missing_team_id(connector):
    query = ConnectorQuery(resource="channels")
    with pytest.raises(ValueError, match="'team_id' in filters"):
        await connector.query(query)


@respx.mock
async def test_query_channel(connector):
    body = {"id": "c1", "displayName": "general"}
    respx.get(f"{_BASE}/teams/t1/channels/c1").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="channel", filters={"team_id": "t1", "channel_id": "c1"}),
    )
    assert len(result.records) == 1


async def test_query_channel_missing_filters(connector):
    query = ConnectorQuery(resource="channel", filters={"team_id": "t1"})
    with pytest.raises(ValueError, match="'team_id' and 'channel_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — messages / channel_messages
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_messages(connector):
    body = {"value": [{"id": "m1", "body": {"content": "hi"}}]}
    respx.get(f"{_BASE}/teams/t1/channels/c1/messages").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="messages", filters={"team_id": "t1", "channel_id": "c1"}),
    )
    assert result.total == 1


async def test_query_messages_missing_filters(connector):
    query = ConnectorQuery(resource="messages", filters={"team_id": "t1"})
    with pytest.raises(ValueError, match="'team_id' and 'channel_id' in filters"):
        await connector.query(query)


@respx.mock
async def test_query_channel_messages(connector):
    body = {"value": [{"id": "m2"}]}
    respx.get(f"{_BASE}/teams/t1/channels/c1/messages").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="channel_messages", filters={"team_id": "t1", "channel_id": "c1"}),
    )
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — members / users / groups
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_members(connector):
    body = {"value": [{"id": "u1", "displayName": "Alice"}]}
    respx.get(f"{_BASE}/teams/t1/members").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="members", filters={"team_id": "t1"}))
    assert result.total == 1


async def test_query_members_missing_team_id(connector):
    query = ConnectorQuery(resource="members")
    with pytest.raises(ValueError, match="'team_id' in filters"):
        await connector.query(query)


@respx.mock
async def test_query_users(connector):
    body = {"value": [{"id": "u1", "displayName": "Alice"}]}
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert result.total == 1


@respx.mock
async def test_query_groups(connector):
    body = {"value": [{"id": "g1", "displayName": "Team"}]}
    respx.get(f"{_BASE}/groups").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="groups"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Microsoft Teams resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — message
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_message(connector):
    created = {"id": "m1", "body": {"content": "hello"}}
    respx.post(f"{_BASE}/teams/t1/channels/c1/messages").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="message", data={"team_id": "t1", "channel_id": "c1", "body": "hello"}),
    )
    assert result["id"] == "m1"


async def test_write_message_missing_fields(connector):
    with pytest.raises(ValueError, match="'team_id', 'channel_id', and 'body' in data"):
        await connector.write(
            ConnectorPayload(resource="message", data={"team_id": "t1", "channel_id": "c1"}),
        )


# ---------------------------------------------------------------------------
# write — channel
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_channel(connector):
    created = {"id": "c2", "displayName": "announcements"}
    respx.post(f"{_BASE}/teams/t1/channels").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="channel", data={"team_id": "t1", "displayName": "announcements"}),
    )
    assert result["id"] == "c2"


async def test_write_channel_missing_fields(connector):
    with pytest.raises(ValueError, match="'team_id' and 'displayName' in data"):
        await connector.write(ConnectorPayload(resource="channel", data={"team_id": "t1"}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Microsoft Teams write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="teams"))
