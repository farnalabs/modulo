"""Step definitions for the Microsoft Teams connector BDD feature.

Wires ``features/connectors/microsoft_teams.feature`` into the executing suite
(the improve-architecture product-map walk) by driving the REAL
``MicrosoftTeamsConnector`` against a respx-mocked Microsoft Graph API v1.0 —
mirroring ``backend/tests/unit/connectors/test_microsoft_teams.py`` so the
executing BDD surface locks the same contract the unit suite does: token
validation via ``/users`` (200 => healthy, 401 => unhealthy), listing teams /
channels / messages / members / users / groups, getting a team and a channel by
id, and the message / channel write family.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/microsoft_teams.feature")

_BASE = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Microsoft Teams connector scenarios."""
    return {}


@given("a Microsoft Teams connector configured with valid credentials")
def teams_connector_valid(ctx: dict) -> None:
    from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector

    ctx["connector"] = MicrosoftTeamsConnector(token="ms_test_token")
    ctx["valid"] = True


@given("a Microsoft Teams connector configured with invalid credentials")
def teams_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector

    ctx["connector"] = MicrosoftTeamsConnector(token="bad_token")
    ctx["valid"] = False


@when("the connector checks health")
def teams_health_check(ctx: dict) -> None:
    response = (
        httpx.Response(200, json={"value": [{"id": "U1"}]})
        if ctx["valid"]
        else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.get(f"{_BASE}/users", params={"$top": 1, "$select": "id"}).mock(return_value=response)
        ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when("the connector queries teams")
def teams_query_teams(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    teams = [
        {"id": "T1", "displayName": "Engineering", "description": "Engineering team"},
        {"id": "T2", "displayName": "Marketing", "description": "Marketing team"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json={"value": teams}))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="teams")))


@when(parsers.parse('the connector queries team with team_id "{team_id}"'))
def teams_query_team(team_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    team = {"id": team_id, "displayName": "Engineering", "description": "Build stuff"}
    with respx.mock:
        respx.get(f"{_BASE}/teams/{team_id}").mock(return_value=httpx.Response(200, json=team))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="team", filters={"team_id": team_id}))
        )


@when(parsers.parse('the connector queries channels for team "{team_id}"'))
def teams_query_channels(team_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    channels = [
        {"id": "C1", "displayName": "General"},
        {"id": "C2", "displayName": "Random"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/teams/{team_id}/channels").mock(return_value=httpx.Response(200, json={"value": channels}))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="channels", filters={"team_id": team_id}))
        )


@when(parsers.parse('the connector queries channel with team_id "{team_id}" and channel_id "{channel_id}"'))
def teams_query_channel(team_id: str, channel_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    channel = {"id": channel_id, "displayName": "General", "description": "General discussions"}
    with respx.mock:
        respx.get(f"{_BASE}/teams/{team_id}/channels/{channel_id}").mock(
            return_value=httpx.Response(200, json=channel)
        )
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(
                ConnectorQuery(resource="channel", filters={"team_id": team_id, "channel_id": channel_id})
            )
        )


@when(parsers.parse('the connector queries messages in team "{team_id}" and channel "{channel_id}"'))
def teams_query_messages(team_id: str, channel_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    messages = [
        {"id": "M1", "body": {"content": "Hello"}},
        {"id": "M2", "body": {"content": "World"}},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json={"value": messages})
        )
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(
                ConnectorQuery(resource="messages", filters={"team_id": team_id, "channel_id": channel_id})
            )
        )


@when(parsers.parse('the connector queries members of team "{team_id}"'))
def teams_query_members(team_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    members = [
        {"id": "M1", "displayName": "Alice"},
        {"id": "M2", "displayName": "Bob"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/teams/{team_id}/members").mock(return_value=httpx.Response(200, json={"value": members}))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="members", filters={"team_id": team_id}))
        )


@when("the connector queries users")
def teams_query_users(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    users = [
        {"id": "U1", "displayName": "Alice", "mail": "alice@example.com"},
        {"id": "U2", "displayName": "Bob", "mail": "bob@example.com"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json={"value": users}))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="users")))


@when("the connector queries groups")
def teams_query_groups(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    groups = [
        {"id": "G1", "displayName": "Sales Team"},
        {"id": "G2", "displayName": "Dev Team"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/groups").mock(return_value=httpx.Response(200, json={"value": groups}))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="groups")))


@when(
    parsers.parse(
        'the connector sends a message "{content}" to team "{team_id}" and channel "{channel_id}"'
    )
)
def teams_write_message(content: str, team_id: str, channel_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(
                201,
                json={"id": "M1", "body": {"contentType": "text", "content": content}},
            )
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(
                    resource="message",
                    data={"team_id": team_id, "channel_id": channel_id, "body": content},
                )
            )
        )


@when(parsers.parse('the connector creates a channel "{name}" in team "{team_id}"'))
def teams_write_channel(name: str, team_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/teams/{team_id}/channels").mock(
            return_value=httpx.Response(
                201,
                json={"id": "C3", "displayName": name, "description": "A new channel"},
            )
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(
                    resource="channel",
                    data={"team_id": team_id, "displayName": name, "description": "A new channel"},
                )
            )
        )


@then(parsers.parse('the health check returns "{status}"'))
def teams_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("the result contains Microsoft Teams teams")
def teams_result_teams(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected team records"
    assert result.records[0]["displayName"] == "Engineering", result.records
    assert result.records[1]["displayName"] == "Marketing", result.records


@then("the result contains the Microsoft Teams team")
def teams_result_team(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected team record"
    assert result.records[0]["displayName"] == "Engineering", result.records


@then("the result contains Microsoft Teams channels")
def teams_result_channels(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected channel records"
    assert result.records[0]["displayName"] == "General", result.records


@then("the result contains the Microsoft Teams channel")
def teams_result_channel(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected channel record"
    assert result.records[0]["displayName"] == "General", result.records


@then("the result contains Microsoft Teams messages")
def teams_result_messages(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected message records"
    assert result.records[0]["body"]["content"] == "Hello", result.records
    assert result.records[1]["body"]["content"] == "World", result.records


@then("the result contains Microsoft Teams members")
def teams_result_members(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected member records"
    assert result.records[0]["displayName"] == "Alice", result.records
    assert result.records[1]["displayName"] == "Bob", result.records


@then("the result contains Microsoft Graph users")
def teams_result_users(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected user records"
    assert result.records[0]["displayName"] == "Alice", result.records
    assert result.records[1]["displayName"] == "Bob", result.records


@then("the result contains Microsoft Graph groups")
def teams_result_groups(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected group records"
    assert result.records[0]["displayName"] == "Sales Team", result.records


@then("the message is sent successfully")
def teams_message_sent(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["id"] == "M1", result
    assert result["body"]["content"] == "Hello from Modulo", result


@then("the channel is created successfully")
def teams_channel_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["id"] == "C3", result
    assert result["displayName"] == "Announcements", result
