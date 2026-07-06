"""Unit tests for SlackConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.slack import SlackConnector, _parse_retry_after

TOKEN = "xoxb-test-token"


@pytest.fixture()
def connector():
    return SlackConnector(bot_token=TOKEN)


# -- health_check --


@respx.mock
async def test_health_check_ok(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "user_id": "U001"}),
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
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_health_check_non_json(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    result = await connector.health_check()
    assert result.ok is False


# -- query: channels --


@respx.mock
async def test_query_channels(connector):
    channels = [
        {
            "id": "C001",
            "name": "general",
            "topic": {"value": "General chat"},
            "purpose": {"value": ""},
            "num_members": 42,
        },
        {
            "id": "C002",
            "name": "random",
            "topic": {"value": "Random stuff"},
            "purpose": {"value": ""},
            "num_members": 15,
        },
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
async def test_query_channels_http_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 403"):
        await connector.query(ConnectorQuery(resource="channels"))


# -- query: messages --


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


# -- query: users --


@respx.mock
async def test_query_users(connector):
    members = [
        {
            "id": "U001",
            "name": "alice",
            "profile": {"display_name": "Alice", "real_name": "Alice Smith", "email": "alice@example.com"},
            "tz": "America/New_York",
        },
        {
            "id": "U002",
            "name": "bob",
            "profile": {"display_name": "Bob", "real_name": "Bob Jones", "email": "bob@example.com"},
            "tz": "America/Chicago",
        },
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


@respx.mock
async def test_query_users_http_error(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 403"):
        await connector.query(ConnectorQuery(resource="users"))


# -- query: unsupported resource --


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Slack resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# -- write: unsupported resource --


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Slack write resource"):
        await connector.write(ConnectorPayload(resource="file", data={}))


# -- write: message --


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
    with pytest.raises(ValueError, match="Slack API HTTP 500"):
        await connector.write(
            ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"}),
        )


# -- rate limiting (old-style direct raises via ValueError) --


@respx.mock
async def test_query_channels_rate_limited(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_query_users_rate_limited(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.query(ConnectorQuery(resource="users"))


@respx.mock
async def test_query_messages_rate_limited(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.query(
            ConnectorQuery(resource="messages", filters={"channel": "C12345"}),
        )


@respx.mock
async def test_write_message_rate_limited(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.write(
            ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"}),
        )


# -- connector type --


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SLACK


# -- retry/backoff for 429 --


@respx.mock
async def test_429_retry_then_succeed(connector):
    route = respx.get("https://slack.com/api/conversations.list")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, text=""),
        httpx.Response(200, json={"ok": True, "channels": [{"id": "C001", "name": "retried"}]}),
    ]
    result = await connector.query(ConnectorQuery(resource="channels"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "retried"
    assert route.call_count == 2


@respx.mock
async def test_429_retry_exhausted(connector):
    route = respx.get("https://slack.com/api/conversations.list")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, text=""),
        httpx.Response(429, headers={"Retry-After": "0"}, text=""),
        httpx.Response(429, headers={"Retry-After": "0"}, text=""),
        httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    ]
    with pytest.raises(ValueError, match="Slack API HTTP 429"):
        await connector.query(ConnectorQuery(resource="channels"))
    assert route.call_count == 4


# -- connection errors / timeouts --


@respx.mock
async def test_query_channels_connection_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    with pytest.raises(ValueError, match="Slack API connection error"):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_query_messages_timeout_error(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        side_effect=httpx.TimeoutException("Request timed out"),
    )
    with pytest.raises(ValueError, match="Slack API timeout"):
        await connector.query(
            ConnectorQuery(resource="messages", filters={"channel": "C12345"}),
        )


@respx.mock
async def test_write_message_connection_error(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    with pytest.raises(ValueError, match="Slack API connection error"):
        await connector.write(
            ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"}),
        )


@respx.mock
async def test_list_users_connection_error(connector):
    respx.get("https://slack.com/api/users.list").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    with pytest.raises(ValueError, match="Slack API connection error"):
        await connector.query(ConnectorQuery(resource="users"))


# -- verify_scopes --


@respx.mock
async def test_verify_scopes_ok(connector):
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "user_id": "U001", "team": "T001", "url": "https://example.slack.com"}
        ),
    )
    result = await connector.verify_scopes()
    assert result["user_id"] == "U001"
    assert result["team"] == "T001"


@respx.mock
async def test_verify_scopes_fail(connector):
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
    )
    with pytest.raises(ValueError, match="Token validation failed"):
        await connector.verify_scopes()


@respx.mock
async def test_verify_scopes_http_error(connector):
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 403"):
        await connector.verify_scopes()


@respx.mock
async def test_health_check_revoked_token(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "token_revoked"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "token_revoked" in result.detail or "revoked" in result.detail


# -- channel_info --


@respx.mock
async def test_channel_info(connector):
    respx.get("https://slack.com/api/conversations.info").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "channel": {"id": "C001", "name": "general", "topic": {"value": "General chat"}, "num_members": 42},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_info", filters={"channel": "C001"}),
    )
    assert len(result.records) == 1
    assert result.records[0]["name"] == "general"
    assert result.records[0]["num_members"] == 42


@respx.mock
async def test_channel_info_missing_channel(connector):
    with pytest.raises(ValueError, match="requires 'channel' filter"):
        await connector.query(ConnectorQuery(resource="channel_info"))


@respx.mock
async def test_channel_info_api_error(connector):
    respx.get("https://slack.com/api/conversations.info").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    with pytest.raises(ValueError, match="channel_not_found"):
        await connector.query(
            ConnectorQuery(resource="channel_info", filters={"channel": "C99999"}),
        )


# -- channel_members --


@respx.mock
async def test_channel_members(connector):
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "members": ["U001", "U002", "U003"],
                "response_metadata": {"next_cursor": ""},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
    )
    assert len(result.records) == 3
    assert result.records[0]["user_id"] == "U001"
    assert result.next_cursor == ""


@respx.mock
async def test_channel_members_with_cursor(connector):
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "members": ["U004", "U005"],
                "response_metadata": {"next_cursor": "page2"},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_members", filters={"channel": "C001"}, cursor="page1"),
    )
    assert len(result.records) == 2
    assert result.next_cursor == "page2"


@respx.mock
async def test_channel_members_missing_channel(connector):
    with pytest.raises(ValueError, match="requires 'channel' filter"):
        await connector.query(ConnectorQuery(resource="channel_members"))


@respx.mock
async def test_channel_members_api_error(connector):
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"}),
    )
    with pytest.raises(ValueError, match="not_in_channel"):
        await connector.query(
            ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
        )


# -- thread_replies --


@respx.mock
async def test_thread_replies(connector):
    replies = [
        {"ts": "123456.000001", "text": "Original", "user": "U001"},
        {"ts": "123456.000002", "text": "Reply 1", "user": "U002"},
    ]
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": replies,
                "response_metadata": {"next_cursor": ""},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="thread_replies", filters={"channel": "C001", "thread_ts": "123456.000001"}),
    )
    assert len(result.records) == 2
    assert result.records[1]["text"] == "Reply 1"


@respx.mock
async def test_thread_replies_missing_channel(connector):
    with pytest.raises(ValueError, match="requires 'channel' filter"):
        await connector.query(
            ConnectorQuery(resource="thread_replies", filters={"thread_ts": "123456.000001"}),
        )


@respx.mock
async def test_thread_replies_missing_thread_ts(connector):
    with pytest.raises(ValueError, match="requires 'thread_ts' filter"):
        await connector.query(
            ConnectorQuery(resource="thread_replies", filters={"channel": "C001"}),
        )


@respx.mock
async def test_thread_replies_api_error(connector):
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "thread_not_found"}),
    )
    with pytest.raises(ValueError, match="thread_not_found"):
        await connector.query(
            ConnectorQuery(resource="thread_replies", filters={"channel": "C001", "thread_ts": "999999.000000"}),
        )


# -- thread_reply (write) --


@respx.mock
async def test_thread_reply_write(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "888777", "channel": "C001"}),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="thread_reply",
            data={
                "channel": "C001",
                "thread_ts": "123456.000001",
                "text": "A reply",
            },
        ),
    )
    assert result["ts"] == "888777"
    assert result["channel"] == "C001"


@respx.mock
async def test_thread_reply_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in thread_reply"):
        await connector.write(
            ConnectorPayload(resource="thread_reply", data={"thread_ts": "123456.000001", "text": "Hello"}),
        )


@respx.mock
async def test_thread_reply_missing_thread_ts(connector):
    with pytest.raises(ValueError, match="Missing 'thread_ts' in thread_reply"):
        await connector.write(
            ConnectorPayload(resource="thread_reply", data={"channel": "C001", "text": "Hello"}),
        )


@respx.mock
async def test_thread_reply_api_error(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_arguments"}),
    )
    with pytest.raises(ValueError, match="invalid_arguments"):
        await connector.write(
            ConnectorPayload(
                resource="thread_reply",
                data={
                    "channel": "C001",
                    "thread_ts": "123456.000001",
                    "text": "Hello",
                },
            ),
        )


# -- JSON decode error in query/write --


@respx.mock
async def test_query_channels_json_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    with pytest.raises(ValueError, match="invalid JSON"):
        await connector.query(ConnectorQuery(resource="channels"))


@respx.mock
async def test_parse_retry_after_valid():
    resp = httpx.Response(429, headers={"Retry-After": "12.5"})
    assert _parse_retry_after(resp) == 12.5


def test_parse_retry_after_missing():
    resp = httpx.Response(200)
    assert _parse_retry_after(resp) is None


def test_parse_retry_after_invalid():
    resp = httpx.Response(429, headers={"Retry-After": "not-a-number"})
    assert _parse_retry_after(resp) is None
