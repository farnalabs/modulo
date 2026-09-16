"""Step definitions for the Discord connector BDD feature.

Wires ``features/connectors/discord.feature`` into the executing suite (the
improve-architecture product-map walk) by driving the REAL ``DiscordConnector``
against a respx-mocked Discord REST API v10 — mirroring
``backend/tests/unit/connectors/test_discord.py`` so the executing BDD surface
locks the same contract the unit suite does: bot-token validation via
``/users/@me`` (200 => healthy, 401 => unhealthy), listing guilds / channels /
messages / guild members / roles, getting a guild by id, and the message /
reaction / channel write family.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/discord.feature")

_BASE = "https://discord.com/api/v10"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Discord connector scenarios."""
    return {}


@given("a Discord connector configured with valid credentials")
def discord_connector_valid(ctx: dict) -> None:
    from modulo.connectors.discord import DiscordConnector

    ctx["connector"] = DiscordConnector(token="discord_bot_token")
    ctx["valid"] = True


@given("a Discord connector configured with invalid credentials")
def discord_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.discord import DiscordConnector

    ctx["connector"] = DiscordConnector(token="bad_token")
    ctx["valid"] = False


@when("the connector checks health")
def discord_health_check(ctx: dict) -> None:
    response = (
        httpx.Response(200, json={"id": "123", "username": "ModuloBot"})
        if ctx["valid"]
        else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.get(f"{_BASE}/users/@me").mock(return_value=response)
        ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when("the connector queries guilds")
def discord_query_guilds(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    guilds = [
        {"id": "111", "name": "Modulo Dev"},
        {"id": "222", "name": "Modulo Ops"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/users/@me/guilds").mock(return_value=httpx.Response(200, json=guilds))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="guilds")))


@when(parsers.parse('the connector queries channels for guild "{guild_id}"'))
def discord_query_channels(guild_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    channels = [
        {"id": "333", "name": "general", "type": 0},
        {"id": "444", "name": "random", "type": 0},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/guilds/{guild_id}/channels").mock(return_value=httpx.Response(200, json=channels))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="channels", filters={"guild_id": guild_id}))
        )


@when(parsers.parse('the connector queries messages in channel "{channel_id}"'))
def discord_query_messages(channel_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    messages = [
        {"id": "555", "content": "Hello", "author": {"id": "U1"}},
        {"id": "666", "content": "World", "author": {"id": "U2"}},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/channels/{channel_id}/messages").mock(return_value=httpx.Response(200, json=messages))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="messages", filters={"channel_id": channel_id}))
        )


@when(parsers.parse('the connector queries members of guild "{guild_id}"'))
def discord_query_members(guild_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    members = [
        {"user": {"id": "U1", "username": "Alice"}, "roles": []},
        {"user": {"id": "U2", "username": "Bob"}, "roles": ["R1"]},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/guilds/{guild_id}/members").mock(return_value=httpx.Response(200, json=members))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="guild_members", filters={"guild_id": guild_id}))
        )


@when(parsers.parse('the connector queries roles for guild "{guild_id}"'))
def discord_query_roles(guild_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    roles = [
        {"id": "R1", "name": "Admin", "color": 0xFF0000},
        {"id": "R2", "name": "Mod", "color": 0x00FF00},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/guilds/{guild_id}/roles").mock(return_value=httpx.Response(200, json=roles))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="roles", filters={"guild_id": guild_id}))
        )


@when(parsers.parse('the connector queries guild with guild_id "{guild_id}"'))
def discord_query_guild(guild_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    guild = {"id": guild_id, "name": "Modulo Dev", "member_count": 42}
    with respx.mock:
        respx.get(f"{_BASE}/guilds/{guild_id}").mock(return_value=httpx.Response(200, json=guild))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="guild", filters={"guild_id": guild_id}))
        )


@when(parsers.parse('the connector sends a message "{content}" to channel "{channel_id}"'))
def discord_write_message(content: str, channel_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-001", "channel_id": channel_id, "content": content})
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(resource="message", data={"channel_id": channel_id, "content": content})
            )
        )


@when(parsers.parse('the connector adds a reaction "{emoji}" to message "{message_id}" in channel "{channel_id}"'))
def discord_write_reaction(emoji: str, message_id: str, channel_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.put(f"{_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me").mock(
            return_value=httpx.Response(204)
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(
                    resource="reaction",
                    data={"channel_id": channel_id, "message_id": message_id, "emoji": emoji},
                )
            )
        )


@when(parsers.parse('the connector creates a channel "{name}" in guild "{guild_id}"'))
def discord_write_channel(name: str, guild_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(201, json={"id": "ch-new", "name": name, "type": 0, "guild_id": guild_id})
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(resource="channel", data={"guild_id": guild_id, "name": name})
            )
        )


@then(parsers.parse('the health check returns "{status}"'))
def discord_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("the result contains Discord guilds")
def discord_result_guilds(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected guild records"
    assert result.records[0]["name"] == "Modulo Dev", result.records
    assert result.records[1]["name"] == "Modulo Ops", result.records


@then("the result contains Discord channels")
def discord_result_channels(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected channel records"
    assert result.records[0]["name"] == "general", result.records


@then("the result contains Discord messages")
def discord_result_messages(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected message records"
    assert result.records[0]["content"] == "Hello", result.records
    assert result.records[1]["content"] == "World", result.records


@then("the result contains Discord guild members")
def discord_result_members(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected member records"
    assert result.records[0]["user"]["username"] == "Alice", result.records
    assert result.records[1]["user"]["username"] == "Bob", result.records


@then("the result contains Discord roles")
def discord_result_roles(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected role records"
    assert result.records[0]["name"] == "Admin", result.records


@then("the result contains the Discord guild")
def discord_result_guild(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected guild record"
    assert result.records[0]["name"] == "Modulo Dev", result.records
    assert result.records[0]["member_count"] == 42, result.records


@then("the message is sent successfully")
def discord_message_sent(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["id"] == "msg-001", result
    assert result["content"] == "Hello from Modulo", result


@then("the reaction is added successfully")
def discord_reaction_added(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["ok"] is True, result


@then("the channel is created successfully")
def discord_channel_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["id"] == "ch-new", result
    assert result["name"] == "announcements", result
