"""Unit tests for DiscordConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.discord import DiscordConnector

TOKEN = "discord_test_token"
_BASE = "https://discord.com/api/v10"


@pytest.fixture
def connector():
    return DiscordConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.DISCORD


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"username": "modulo-bot"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "modulo-bot"


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/users/@me").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Discord bot token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/users/@me").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/users/@me").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False


# ---------------------------------------------------------------------------
# query — guilds
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_guilds(connector):
    body = [{"id": "g1", "name": "Engineering"}]
    respx.get(f"{_BASE}/users/@me/guilds").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="guilds"))
    assert result.total == 1
    assert result.records[0]["id"] == "g1"


# ---------------------------------------------------------------------------
# query — channels
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_channels(connector):
    body = [{"id": "c1", "name": "general"}]
    respx.get(f"{_BASE}/guilds/g1/channels").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="channels", filters={"guild_id": "g1"}))
    assert result.records[0]["id"] == "c1"


async def test_query_channels_missing_guild_id(connector):
    query = ConnectorQuery(resource="channels")
    with pytest.raises(ValueError, match="'guild_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — messages
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_messages(connector):
    body = [{"id": "m1", "content": "hello"}]
    respx.get(f"{_BASE}/channels/c1/messages").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="messages", filters={"channel_id": "c1", "around": "123"}),
    )
    assert result.total == 1
    assert result.records[0]["id"] == "m1"


async def test_query_messages_missing_channel_id(connector):
    query = ConnectorQuery(resource="messages")
    with pytest.raises(ValueError, match="'channel_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — guild_members / roles / guild
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_guild_members(connector):
    body = [{"user": {"username": "alice"}}]
    respx.get(f"{_BASE}/guilds/g1/members").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="guild_members", filters={"guild_id": "g1"}))
    assert result.total == 1


async def test_query_guild_members_missing_guild_id(connector):
    query = ConnectorQuery(resource="guild_members")
    with pytest.raises(ValueError, match="'guild_id' in filters"):
        await connector.query(query)


@respx.mock
async def test_query_roles(connector):
    body = [{"id": "r1", "name": "admin"}]
    respx.get(f"{_BASE}/guilds/g1/roles").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="roles", filters={"guild_id": "g1"}))
    assert result.total == 1


async def test_query_roles_missing_guild_id(connector):
    query = ConnectorQuery(resource="roles")
    with pytest.raises(ValueError, match="'guild_id' in filters"):
        await connector.query(query)


@respx.mock
async def test_query_guild(connector):
    body = {"id": "g1", "name": "Engineering"}
    respx.get(f"{_BASE}/guilds/g1").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="guild", filters={"guild_id": "g1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Engineering"


async def test_query_guild_missing_guild_id(connector):
    query = ConnectorQuery(resource="guild")
    with pytest.raises(ValueError, match="'guild_id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Discord resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — message
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_message(connector):
    body = {"id": "m1", "channel_id": "c1", "content": "hi"}
    respx.post(f"{_BASE}/channels/c1/messages").mock(return_value=httpx.Response(200, json=body))
    result = await connector.write(
        ConnectorPayload(resource="message", data={"channel_id": "c1", "content": "hi"}),
    )
    assert result["id"] == "m1"


async def test_write_message_missing_fields(connector):
    with pytest.raises(ValueError, match="'channel_id' and 'content' in data"):
        await connector.write(ConnectorPayload(resource="message", data={"channel_id": "c1"}))


# ---------------------------------------------------------------------------
# write — reaction
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_reaction(connector):
    respx.put(f"{_BASE}/channels/c1/messages/m1/reactions/%E2%9C%85/@me").mock(
        return_value=httpx.Response(204),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="reaction",
            data={"channel_id": "c1", "message_id": "m1", "emoji": "%E2%9C%85"},
        ),
    )
    assert result["ok"] is True


async def test_write_reaction_missing_fields(connector):
    with pytest.raises(ValueError, match="'channel_id', 'message_id', and 'emoji' in data"):
        await connector.write(ConnectorPayload(resource="reaction", data={"channel_id": "c1"}))


# ---------------------------------------------------------------------------
# write — channel
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_channel(connector):
    body = {"id": "c2", "name": "announcements"}
    respx.post(f"{_BASE}/guilds/g1/channels").mock(return_value=httpx.Response(200, json=body))
    result = await connector.write(
        ConnectorPayload(resource="channel", data={"guild_id": "g1", "name": "announcements"}),
    )
    assert result["id"] == "c2"


async def test_write_channel_missing_fields(connector):
    with pytest.raises(ValueError, match="'guild_id' and 'name' in data"):
        await connector.write(ConnectorPayload(resource="channel", data={"guild_id": "g1"}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Discord write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/users/@me/guilds").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="guilds"))
