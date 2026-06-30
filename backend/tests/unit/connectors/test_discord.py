"""Unit tests for DiscordConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.discord import DiscordConnector

TOKEN = "discord_bot_token"
_BASE = "https://discord.com/api/v10"


@pytest.fixture()
def connector() -> DiscordConnector:
    return DiscordConnector(token=TOKEN)


def test_connector_type(connector: DiscordConnector) -> None:
    assert connector.connector_type == ConnectorType.DISCORD


@respx.mock
async def test_health_check_ok(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me").mock(
        return_value=httpx.Response(200, json={"id": "123", "username": "ModuloBot"})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "ModuloBot"


@respx.mock
async def test_health_check_invalid_token(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Discord bot token" in result.detail


@respx.mock
async def test_health_check_network_error(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_other_status(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_query_guilds(connector: DiscordConnector) -> None:
    guilds = [
        {"id": "111", "name": "Modulo Dev"},
        {"id": "222", "name": "Modulo Ops"},
    ]
    respx.get(f"{_BASE}/users/@me/guilds").mock(
        return_value=httpx.Response(200, json=guilds)
    )
    result = await connector.query(ConnectorQuery(resource="guilds"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Modulo Dev"


@respx.mock
async def test_query_guilds_with_limit(connector: DiscordConnector) -> None:
    guilds = [{"id": str(i), "name": f"Guild {i}"} for i in range(5)]
    respx.get(f"{_BASE}/users/@me/guilds", params={"limit": 5}).mock(
        return_value=httpx.Response(200, json=guilds)
    )
    result = await connector.query(ConnectorQuery(resource="guilds", limit=5))
    assert len(result.records) == 5


@respx.mock
async def test_query_channels(connector: DiscordConnector) -> None:
    channels = [
        {"id": "333", "name": "general", "type": 0},
        {"id": "444", "name": "random", "type": 0},
    ]
    guild_id = "guild-123"
    respx.get(f"{_BASE}/guilds/{guild_id}/channels").mock(
        return_value=httpx.Response(200, json=channels)
    )
    result = await connector.query(
        ConnectorQuery(resource="channels", filters={"guild_id": guild_id})
    )
    assert len(result.records) == 2
    assert result.records[0]["name"] == "general"


@respx.mock
async def test_query_channels_missing_guild_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord channels query requires 'guild_id' in filters"):
        await connector.query(ConnectorQuery(resource="channels", filters={}))


@respx.mock
async def test_query_messages(connector: DiscordConnector) -> None:
    messages = [
        {"id": "555", "content": "Hello", "author": {"id": "U1"}},
        {"id": "666", "content": "World", "author": {"id": "U2"}},
    ]
    channel_id = "ch-789"
    respx.get(f"{_BASE}/channels/{channel_id}/messages", params={"limit": 100}).mock(
        return_value=httpx.Response(200, json=messages)
    )
    result = await connector.query(
        ConnectorQuery(resource="messages", filters={"channel_id": channel_id})
    )
    assert len(result.records) == 2
    assert result.records[0]["content"] == "Hello"


@respx.mock
async def test_query_messages_with_limit_and_before(connector: DiscordConnector) -> None:
    messages = [{"id": "777", "content": "Limited"}]
    channel_id = "ch-789"
    respx.get(
        f"{_BASE}/channels/{channel_id}/messages",
        params={"limit": 1, "before": "666"},
    ).mock(return_value=httpx.Response(200, json=messages))
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"channel_id": channel_id, "before": "666"},
            limit=1,
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_messages_with_after(connector: DiscordConnector) -> None:
    messages = [{"id": "888", "content": "After msg"}]
    channel_id = "ch-789"
    respx.get(
        f"{_BASE}/channels/{channel_id}/messages",
        params={"limit": 100, "after": "555"},
    ).mock(return_value=httpx.Response(200, json=messages))
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"channel_id": channel_id, "after": "555"},
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_messages_with_around(connector: DiscordConnector) -> None:
    messages = [{"id": "999", "content": "Around msg"}]
    channel_id = "ch-789"
    respx.get(
        f"{_BASE}/channels/{channel_id}/messages",
        params={"limit": 100, "around": "555"},
    ).mock(return_value=httpx.Response(200, json=messages))
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"channel_id": channel_id, "around": "555"},
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_messages_missing_channel_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord messages query requires 'channel_id' in filters"):
        await connector.query(ConnectorQuery(resource="messages", filters={}))


@respx.mock
async def test_query_guild_members(connector: DiscordConnector) -> None:
    members = [
        {"user": {"id": "U1", "username": "Alice"}, "roles": []},
        {"user": {"id": "U2", "username": "Bob"}, "roles": ["R1"]},
    ]
    guild_id = "guild-123"
    respx.get(f"{_BASE}/guilds/{guild_id}/members", params={"limit": 100}).mock(
        return_value=httpx.Response(200, json=members)
    )
    result = await connector.query(
        ConnectorQuery(resource="guild_members", filters={"guild_id": guild_id})
    )
    assert len(result.records) == 2
    assert result.records[0]["user"]["username"] == "Alice"


@respx.mock
async def test_query_guild_members_missing_guild_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord guild_members query requires 'guild_id' in filters"):
        await connector.query(ConnectorQuery(resource="guild_members", filters={}))


@respx.mock
async def test_query_roles(connector: DiscordConnector) -> None:
    roles = [
        {"id": "R1", "name": "Admin", "color": 0xFF0000},
        {"id": "R2", "name": "Mod", "color": 0x00FF00},
    ]
    guild_id = "guild-123"
    respx.get(f"{_BASE}/guilds/{guild_id}/roles").mock(
        return_value=httpx.Response(200, json=roles)
    )
    result = await connector.query(
        ConnectorQuery(resource="roles", filters={"guild_id": guild_id})
    )
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Admin"


@respx.mock
async def test_query_roles_missing_guild_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord roles query requires 'guild_id' in filters"):
        await connector.query(ConnectorQuery(resource="roles", filters={}))


@respx.mock
async def test_query_guild(connector: DiscordConnector) -> None:
    guild = {"id": "guild-123", "name": "Modulo Dev", "member_count": 42}
    guild_id = "guild-123"
    respx.get(f"{_BASE}/guilds/{guild_id}").mock(
        return_value=httpx.Response(200, json=guild)
    )
    result = await connector.query(
        ConnectorQuery(resource="guild", filters={"guild_id": guild_id})
    )
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Modulo Dev"


@respx.mock
async def test_query_guild_missing_guild_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord guild query requires 'guild_id' in filters"):
        await connector.query(ConnectorQuery(resource="guild", filters={}))


async def test_query_invalid_resource(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Discord resource"):
        await connector.query(ConnectorQuery(resource="invalid_thing"))


async def test_write_invalid_resource(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Discord write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_write_message(connector: DiscordConnector) -> None:
    channel_id = "ch-456"
    respx.post(f"{_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "msg-001",
                "channel_id": channel_id,
                "content": "Hello from Modulo",
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="message",
            data={"channel_id": channel_id, "content": "Hello from Modulo"},
        )
    )
    assert result["id"] == "msg-001"
    assert result["content"] == "Hello from Modulo"


@respx.mock
async def test_write_message_with_embed(connector: DiscordConnector) -> None:
    channel_id = "ch-456"
    embed = {"title": "Test Embed", "description": "Embed description", "color": 0x00FF00}
    respx.post(f"{_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "msg-002",
                "channel_id": channel_id,
                "content": "With embed",
                "embeds": [embed],
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="message",
            data={
                "channel_id": channel_id,
                "content": "With embed",
                "embed": embed,
            },
        )
    )
    assert result["id"] == "msg-002"


@respx.mock
async def test_write_message_missing_channel_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord message write requires 'channel_id' and 'content' in data"):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"content": "Hello"},
            )
        )


@respx.mock
async def test_write_message_missing_content(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord message write requires 'channel_id' and 'content' in data"):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"channel_id": "ch-456"},
            )
        )


@respx.mock
async def test_write_reaction(connector: DiscordConnector) -> None:
    channel_id = "ch-456"
    message_id = "msg-789"
    emoji = "👍"
    respx.put(
        f"{_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"
    ).mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="reaction",
            data={
                "channel_id": channel_id,
                "message_id": message_id,
                "emoji": emoji,
            },
        )
    )
    assert result["ok"] is True


@respx.mock
async def test_write_reaction_missing_fields(connector: DiscordConnector) -> None:
    msg = "Discord reaction write requires 'channel_id', 'message_id', and 'emoji' in data"
    with pytest.raises(ValueError, match=msg):
        await connector.write(
            ConnectorPayload(resource="reaction", data={"channel_id": "ch-456"})
        )


@respx.mock
async def test_write_reaction_missing_emoji(connector: DiscordConnector) -> None:
    msg = "Discord reaction write requires 'channel_id', 'message_id', and 'emoji' in data"
    with pytest.raises(ValueError, match=msg):
        await connector.write(
            ConnectorPayload(
                resource="reaction",
                data={"channel_id": "ch-456", "message_id": "msg-789"},
            )
        )


@respx.mock
async def test_write_channel(connector: DiscordConnector) -> None:
    guild_id = "guild-123"
    respx.post(f"{_BASE}/guilds/{guild_id}/channels").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "ch-new",
                "name": "announcements",
                "type": 0,
                "guild_id": guild_id,
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="channel",
            data={"guild_id": guild_id, "name": "announcements"},
        )
    )
    assert result["id"] == "ch-new"
    assert result["name"] == "announcements"


@respx.mock
async def test_write_channel_with_topic(connector: DiscordConnector) -> None:
    guild_id = "guild-123"
    respx.post(f"{_BASE}/guilds/{guild_id}/channels").mock(
        return_value=httpx.Response(
            201,
            json={"id": "ch-topic", "name": "updates", "topic": "Project updates"},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="channel",
            data={"guild_id": guild_id, "name": "updates", "topic": "Project updates"},
        )
    )
    assert result["topic"] == "Project updates"


@respx.mock
async def test_write_channel_with_type(connector: DiscordConnector) -> None:
    guild_id = "guild-123"
    respx.post(f"{_BASE}/guilds/{guild_id}/channels").mock(
        return_value=httpx.Response(
            201,
            json={"id": "ch-voice", "name": "Voice", "type": 2},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="channel",
            data={"guild_id": guild_id, "name": "Voice", "type": 2},
        )
    )
    assert result["type"] == 2


@respx.mock
async def test_write_channel_missing_guild_id(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord channel write requires 'guild_id' and 'name' in data"):
        await connector.write(
            ConnectorPayload(
                resource="channel",
                data={"name": "announcements"},
            )
        )


@respx.mock
async def test_write_channel_missing_name(connector: DiscordConnector) -> None:
    with pytest.raises(ValueError, match="Discord channel write requires 'guild_id' and 'name' in data"):
        await connector.write(
            ConnectorPayload(
                resource="channel",
                data={"guild_id": "guild-123"},
            )
        )


@respx.mock
async def test_query_http_401(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me/guilds").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="guilds"))


@respx.mock
async def test_query_http_429(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me/guilds").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="guilds"))


@respx.mock
async def test_query_http_500(connector: DiscordConnector) -> None:
    respx.get(f"{_BASE}/users/@me/guilds").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="guilds"))


@respx.mock
async def test_write_http_401(connector: DiscordConnector) -> None:
    channel_id = "ch-456"
    respx.post(f"{_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"channel_id": channel_id, "content": "Hello"},
            )
        )


@respx.mock
async def test_write_http_429(connector: DiscordConnector) -> None:
    channel_id = "ch-456"
    respx.post(f"{_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"channel_id": channel_id, "content": "Hello"},
            )
        )


@respx.mock
async def test_write_http_500(connector: DiscordConnector) -> None:
    channel_id = "ch-456"
    respx.post(f"{_BASE}/channels/{channel_id}/messages").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="message",
                data={"channel_id": channel_id, "content": "Hello"},
            )
        )
