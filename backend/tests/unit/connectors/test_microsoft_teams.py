"""Unit tests for MicrosoftTeamsConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector

TOKEN = "ms_test_token"
_BASE = "https://graph.microsoft.com/v1.0"


@pytest.fixture()
def connector() -> MicrosoftTeamsConnector:
    return MicrosoftTeamsConnector(token=TOKEN)


def test_connector_type(connector: MicrosoftTeamsConnector) -> None:
    assert connector.connector_type == ConnectorType.MICROSOFT_TEAMS


@respx.mock
async def test_health_check_ok(connector: MicrosoftTeamsConnector) -> None:
    respx.get(f"{_BASE}/users", params={"$top": 1, "$select": "id"}).mock(
        return_value=httpx.Response(200, json={"value": [{"id": "U1"}]})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Microsoft Graph API token validated"


@respx.mock
async def test_health_check_invalid_token(connector: MicrosoftTeamsConnector) -> None:
    respx.get(f"{_BASE}/users", params={"$top": 1, "$select": "id"}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Microsoft Graph API token" in result.detail


@respx.mock
async def test_health_check_network_error(connector: MicrosoftTeamsConnector) -> None:
    respx.get(f"{_BASE}/users", params={"$top": 1, "$select": "id"}).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_other_status(connector: MicrosoftTeamsConnector) -> None:
    respx.get(f"{_BASE}/users", params={"$top": 1, "$select": "id"}).mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_query_teams(connector: MicrosoftTeamsConnector) -> None:
    teams = [
        {"id": "T1", "displayName": "Engineering", "description": "Engineering team"},
        {"id": "T2", "displayName": "Marketing", "description": "Marketing team"},
    ]
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json={"value": teams}))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "Engineering"


@respx.mock
async def test_query_teams_with_filters(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/teams",
        params={"$filter": "startswith(displayName,'Eng')", "$top": 5},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": "T1", "displayName": "Engineering"}]},
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="teams",
            filters={"$filter": "startswith(displayName,'Eng')"},
            limit=5,
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "T1"


@respx.mock
async def test_query_teams_with_limit(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/teams",
        params={"$top": 3},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": f"T{i}", "displayName": f"Team {i}"} for i in range(10)]},
        )
    )
    result = await connector.query(ConnectorQuery(resource="teams", limit=3))
    assert len(result.records) == 3


@respx.mock
async def test_query_teams_with_cursor(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/teams",
        params={"$skiptoken": "token123", "$top": 5},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"id": "T5", "displayName": "Team 5"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams?$skiptoken=nexttoken&$top=5",
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="teams", cursor="token123", limit=5))
    assert len(result.records) == 1
    assert result.next_cursor == "nexttoken"


@respx.mock
async def test_query_team_by_id(connector: MicrosoftTeamsConnector) -> None:
    team_data = {"id": "T1", "displayName": "Engineering", "description": "Build stuff"}
    respx.get(f"{_BASE}/teams/T1").mock(return_value=httpx.Response(200, json=team_data))
    result = await connector.query(ConnectorQuery(resource="team", filters={"team_id": "T1"}))
    assert len(result.records) == 1
    assert result.records[0]["displayName"] == "Engineering"


@respx.mock
async def test_query_team_missing_team_id(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Microsoft Teams team query requires 'team_id' in filters"):
        await connector.query(ConnectorQuery(resource="team"))


@respx.mock
async def test_query_channels(connector: MicrosoftTeamsConnector) -> None:
    channels = [
        {"id": "C1", "displayName": "General"},
        {"id": "C2", "displayName": "Random"},
    ]
    respx.get(f"{_BASE}/teams/T1/channels").mock(return_value=httpx.Response(200, json={"value": channels}))
    result = await connector.query(ConnectorQuery(resource="channels", filters={"team_id": "T1"}))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "General"


@respx.mock
async def test_query_channels_missing_team_id(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Microsoft Teams channels query requires 'team_id' in filters"):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_query_channel_by_id(connector: MicrosoftTeamsConnector) -> None:
    channel_data = {"id": "C1", "displayName": "General", "description": "General discussions"}
    respx.get(f"{_BASE}/teams/T1/channels/C1").mock(return_value=httpx.Response(200, json=channel_data))
    result = await connector.query(
        ConnectorQuery(
            resource="channel",
            filters={"team_id": "T1", "channel_id": "C1"},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["displayName"] == "General"


@respx.mock
async def test_query_channel_missing_ids(connector: MicrosoftTeamsConnector) -> None:
    msg = "Microsoft Teams channel query requires 'team_id' and 'channel_id' in filters"
    with pytest.raises(ValueError, match=msg):
        await connector.query(ConnectorQuery(resource="channel", filters={"team_id": "T1"}))


@respx.mock
async def test_query_channel_missing_both_ids(connector: MicrosoftTeamsConnector) -> None:
    msg = "Microsoft Teams channel query requires 'team_id' and 'channel_id' in filters"
    with pytest.raises(ValueError, match=msg):
        await connector.query(ConnectorQuery(resource="channel"))


@respx.mock
async def test_query_messages(connector: MicrosoftTeamsConnector) -> None:
    messages = [
        {"id": "M1", "body": {"content": "Hello"}},
        {"id": "M2", "body": {"content": "World"}},
    ]
    respx.get(f"{_BASE}/teams/T1/channels/C1/messages").mock(return_value=httpx.Response(200, json={"value": messages}))
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"team_id": "T1", "channel_id": "C1"},
        )
    )
    assert len(result.records) == 2
    assert result.records[0]["body"]["content"] == "Hello"


@respx.mock
async def test_query_messages_with_orderby(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/teams/T1/channels/C1/messages",
        params={"$orderby": "createdDateTime desc", "$top": 10},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": "M3", "body": {"content": "Newest"}}]},
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"team_id": "T1", "channel_id": "C1", "$orderby": "createdDateTime desc"},
            limit=10,
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_messages_with_limit(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/teams/T1/channels/C1/messages",
        params={"$top": 3},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": f"M{i}"} for i in range(5)]},
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"team_id": "T1", "channel_id": "C1"},
            limit=3,
        )
    )
    assert len(result.records) == 3


@respx.mock
async def test_query_messages_missing_ids(connector: MicrosoftTeamsConnector) -> None:
    msg = "Microsoft Teams messages query requires 'team_id' and 'channel_id' in filters"
    with pytest.raises(ValueError, match=msg):
        await connector.query(ConnectorQuery(resource="messages", filters={"team_id": "T1"}))


@respx.mock
async def test_query_channel_messages(connector: MicrosoftTeamsConnector) -> None:
    messages = [
        {"id": "CM1", "body": {"content": "Channel msg 1"}},
    ]
    respx.get(f"{_BASE}/teams/T1/channels/C1/messages").mock(return_value=httpx.Response(200, json={"value": messages}))
    result = await connector.query(
        ConnectorQuery(
            resource="channel_messages",
            filters={"team_id": "T1", "channel_id": "C1"},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["body"]["content"] == "Channel msg 1"


@respx.mock
async def test_query_members(connector: MicrosoftTeamsConnector) -> None:
    members = [
        {"id": "M1", "displayName": "Alice"},
        {"id": "M2", "displayName": "Bob"},
    ]
    respx.get(f"{_BASE}/teams/T1/members").mock(return_value=httpx.Response(200, json={"value": members}))
    result = await connector.query(ConnectorQuery(resource="members", filters={"team_id": "T1"}))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "Alice"


@respx.mock
async def test_query_members_missing_team_id(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Microsoft Teams members query requires 'team_id' in filters"):
        await connector.query(ConnectorQuery(resource="members"))


@respx.mock
async def test_query_users(connector: MicrosoftTeamsConnector) -> None:
    users = [
        {"id": "U1", "displayName": "Alice", "mail": "alice@example.com"},
        {"id": "U2", "displayName": "Bob", "mail": "bob@example.com"},
    ]
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json={"value": users}))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "Alice"


@respx.mock
async def test_query_users_with_filter(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/users",
        params={"$filter": "startswith(displayName,'Bob')", "$top": 10},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": "U2", "displayName": "Bob"}]},
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="users",
            filters={"$filter": "startswith(displayName,'Bob')"},
            limit=10,
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["displayName"] == "Bob"


@respx.mock
async def test_query_groups(connector: MicrosoftTeamsConnector) -> None:
    groups = [
        {"id": "G1", "displayName": "Sales Team"},
        {"id": "G2", "displayName": "Dev Team"},
    ]
    respx.get(f"{_BASE}/groups").mock(return_value=httpx.Response(200, json={"value": groups}))
    result = await connector.query(ConnectorQuery(resource="groups"))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "Sales Team"


@respx.mock
async def test_query_groups_with_limit(connector: MicrosoftTeamsConnector) -> None:
    respx.get(
        f"{_BASE}/groups",
        params={"$top": 1, "$select": "id,displayName,description"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": "G1", "displayName": "Sales Team"}]},
        )
    )
    result = await connector.query(ConnectorQuery(resource="groups", limit=1))
    assert len(result.records) == 1


@respx.mock
async def test_write_message(connector: MicrosoftTeamsConnector) -> None:
    respx.post(f"{_BASE}/teams/T1/channels/C1/messages").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "M1",
                "body": {
                    "contentType": "text",
                    "content": "Hello world",
                },
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="message",
            data={"team_id": "T1", "channel_id": "C1", "body": "Hello world"},
        )
    )
    assert result["id"] == "M1"
    assert result["body"]["content"] == "Hello world"


@respx.mock
async def test_write_message_html_content_type(connector: MicrosoftTeamsConnector) -> None:
    respx.post(f"{_BASE}/teams/T1/channels/C1/messages").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "M2",
                "body": {
                    "contentType": "html",
                    "content": "<h1>Alert</h1>",
                },
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="message",
            data={
                "team_id": "T1",
                "channel_id": "C1",
                "body": "<h1>Alert</h1>",
                "content_type": "html",
            },
        )
    )
    assert result["id"] == "M2"


@respx.mock
async def test_write_message_missing_fields(connector: MicrosoftTeamsConnector) -> None:
    msg = "Microsoft Teams message write requires 'team_id', 'channel_id', and 'body' in data"
    with pytest.raises(ValueError, match=msg):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"team_id": "T1", "channel_id": "C1"},
            )
        )


@respx.mock
async def test_write_message_missing_all(connector: MicrosoftTeamsConnector) -> None:
    msg = "Microsoft Teams message write requires 'team_id', 'channel_id', and 'body' in data"
    with pytest.raises(ValueError, match=msg):
        await connector.write(ConnectorPayload(resource="message", data={}))


@respx.mock
async def test_write_channel(connector: MicrosoftTeamsConnector) -> None:
    respx.post(f"{_BASE}/teams/T1/channels").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "C3",
                "displayName": "New Channel",
                "description": "A new channel",
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="channel",
            data={
                "team_id": "T1",
                "displayName": "New Channel",
                "description": "A new channel",
            },
        )
    )
    assert result["id"] == "C3"
    assert result["displayName"] == "New Channel"


@respx.mock
async def test_write_channel_without_description(connector: MicrosoftTeamsConnector) -> None:
    respx.post(f"{_BASE}/teams/T1/channels").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "C4",
                "displayName": "Minimal Channel",
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="channel",
            data={
                "team_id": "T1",
                "displayName": "Minimal Channel",
            },
        )
    )
    assert result["id"] == "C4"


@respx.mock
async def test_write_channel_missing_team_id(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Microsoft Teams channel write requires 'team_id' and 'displayName' in data"):
        await connector.write(
            ConnectorPayload(
                resource="channel",
                data={"displayName": "No Team Channel"},
            )
        )


@respx.mock
async def test_write_channel_missing_display_name(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Microsoft Teams channel write requires 'team_id' and 'displayName' in data"):
        await connector.write(
            ConnectorPayload(
                resource="channel",
                data={"team_id": "T1"},
            )
        )


async def test_query_invalid_resource(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Microsoft Teams resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector: MicrosoftTeamsConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Microsoft Teams write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_http_500(connector: MicrosoftTeamsConnector) -> None:
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="teams"))


@respx.mock
async def test_query_http_403(connector: MicrosoftTeamsConnector) -> None:
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="users"))


@respx.mock
async def test_write_http_401(connector: MicrosoftTeamsConnector) -> None:
    respx.post(f"{_BASE}/teams/T1/channels/C1/messages").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"team_id": "T1", "channel_id": "C1", "body": "Test"},
            )
        )


@respx.mock
async def test_write_channel_http_500(connector: MicrosoftTeamsConnector) -> None:
    respx.post(f"{_BASE}/teams/T1/channels").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="channel",
                data={"team_id": "T1", "displayName": "Fail Channel"},
            )
        )
