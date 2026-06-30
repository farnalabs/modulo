"""Unit tests for SlackConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.slack import SlackConnector

TOKEN = "xoxb-test-token"


@pytest.fixture()
def connector():
    return SlackConnector(bot_token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert result.detail == "invalid_auth"


@respx.mock
async def test_health_check_http_error(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_rate_limited(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, text=""),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Rate limited" in result.detail


@respx.mock
async def test_query_channels(connector):
    channels = [
        {"id": "C001", "name": "general", "topic": {"value": "General chat"},
         "purpose": {"value": ""}, "num_members": 42},
        {"id": "C002", "name": "random", "topic": {"value": "Random stuff"},
         "purpose": {"value": ""}, "num_members": 15},
    ]
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "channels": channels, "response_metadata": {"next_cursor": ""}},
        ),
    )
    result = await connector.query(ConnectorQuery(resource="channels", limit=10))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "general"
    assert result.next_cursor == ""


@respx.mock
async def test_query_channels_with_cursor(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": "C003", "name": "next-batch"}],
                "response_metadata": {"next_cursor": "page2"},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="channels", cursor="page1"))
    assert len(result.records) == 1
    assert result.next_cursor == "page2"


@respx.mock
async def test_query_channels_api_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_authed"}),
    )
    with pytest.raises(ValueError, match="not_authed"):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_query_messages(connector):
    messages = [
        {"ts": "123456", "text": "Hello", "user": "U001"},
        {"ts": "123457", "text": "World", "user": "U002"},
    ]
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": messages}),
    )
    result = await connector.query(ConnectorQuery(resource="messages", filters={"channel": "C12345"}))
    assert len(result.records) == 2
    assert result.records[0]["text"] == "Hello"


@respx.mock
async def test_query_messages_with_filters(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []}),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="messages",
            filters={"channel": "C12345", "oldest": "1234567890.000000", "latest": "1234567899.000000"},
        )
    )
    assert len(result.records) == 0


@respx.mock
async def test_query_messages_missing_channel(connector):
    with pytest.raises(ValueError, match="requires 'channel' filter"):
        await connector.query(ConnectorQuery(resource="messages"))


@respx.mock
async def test_query_messages_api_error(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    with pytest.raises(ValueError, match="channel_not_found"):
        await connector.query(ConnectorQuery(resource="messages", filters={"channel": "C99999"}))


@respx.mock
async def test_query_users(connector):
    members = [
        {"id": "U001", "name": "alice",
         "profile": {"display_name": "Alice", "real_name": "Alice Smith",
                     "email": "alice@example.com"},
         "tz": "America/New_York"},
        {"id": "U002", "name": "bob",
         "profile": {"display_name": "Bob", "real_name": "Bob Jones",
                     "email": "bob@example.com"},
         "tz": "America/Chicago"},
    ]
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "members": members}),
    )
    result = await connector.query(ConnectorQuery(resource="users", limit=10))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "alice"


@respx.mock
async def test_query_users_with_cursor(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "members": [{"id": "U003", "name": "charlie"}],
                "response_metadata": {"next_cursor": "next_page"},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="users", cursor="prev_page"))
    assert result.records[0]["name"] == "charlie"
    assert result.next_cursor == "next_page"


@respx.mock
async def test_query_users_api_error(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "token_revoked"}),
    )
    with pytest.raises(ValueError, match="token_revoked"):
        await connector.query(ConnectorQuery(resource="users"))


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Slack resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Slack write resource"):
        await connector.write(ConnectorPayload(resource="file", data={}))


@respx.mock
async def test_write_message(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "999888", "channel": "C12345"}),
    )
    result = await connector.write(
        ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello!"}),
    )
    assert result["ts"] == "999888"
    assert result["channel"] == "C12345"


@respx.mock
async def test_write_message_no_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in message payload"):
        await connector.write(ConnectorPayload(resource="message", data={"text": "Hello"}))


@respx.mock
async def test_write_message_api_error(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "too_many_attachments"}),
    )
    with pytest.raises(ValueError, match="too_many_attachments"):
        await connector.write(
            ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"}),
        )


@respx.mock
async def test_write_message_http_error(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(500, text="Server Error"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"}),
        )


@respx.mock
async def test_query_channels_http_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_query_messages_http_error(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="messages", filters={"channel": "C12345"}))


@respx.mock
async def test_query_users_http_error(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="users"))


@respx.mock
async def test_query_channels_rate_limited(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, text=""),
    )
    with pytest.raises(ValueError, match="Rate limited"):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_query_users_rate_limited(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "15"}, text=""),
    )
    with pytest.raises(ValueError, match="Rate limited"):
        await connector.query(ConnectorQuery(resource="users"))


@respx.mock
async def test_query_messages_rate_limited(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "60"}, text=""),
    )
    with pytest.raises(ValueError, match="Rate limited"):
        await connector.query(
            ConnectorQuery(resource="messages", filters={"channel": "C12345"}),
        )


@respx.mock
async def test_write_message_rate_limited(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, text=""),
    )
    with pytest.raises(ValueError, match="Rate limited"):
        await connector.write(
            ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"}),
        )


@respx.mock
async def test_health_check_non_json(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    result = await connector.health_check()
    assert result.ok is False


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SLACK
