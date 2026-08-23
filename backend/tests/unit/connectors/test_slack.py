"""Unit tests for SlackConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.slack import (
    SlackAPIError,
    SlackAuthError,
    SlackConnector,
    SlackError,
    SlackNetworkError,
    SlackRateLimitError,
    _parse_retry_after,
    _safe_int,
)

TOKEN = "xoxb-test-token"


@pytest.fixture
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
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "channels": [{"id": "C001", "name": "general", "is_member": True}]},
        ),
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
    assert not result.next_cursor


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
async def test_query_channels_dict_cursor_not_emitted(connector):
    """A corrupt dict next_cursor must not be emitted as a pagination cursor."""
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": "C003", "name": "next-batch"}],
                "response_metadata": {"next_cursor": {"page": 2}},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="channels", cursor="page1"))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_channels_non_list_page_no_crash(connector):
    """A corrupt ``channels`` field must fall back to an empty page, not a bare string."""
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "channels": "not-a-list"}),
    )
    result = await connector.query(ConnectorQuery(resource="channels", limit=10))
    assert not result.records
    assert result.next_cursor is None


@respx.mock
async def test_query_users_non_list_page_no_crash(connector):
    """A corrupt ``members`` field must fall back to an empty page, not a bare string."""
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "members": "not-a-list"}),
    )
    result = await connector.query(ConnectorQuery(resource="users", limit=10))
    assert not result.records
    assert result.next_cursor is None


@respx.mock
async def test_query_channel_members_non_list_page_no_crash(connector):
    """A corrupt ``members`` field must fall back to an empty page, not a bare string."""
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(200, json={"ok": True, "members": "not-a-list"}),
    )
    result = await connector.query(ConnectorQuery(resource="channel_members", filters={"channel": "C12345"}))
    assert not result.records
    assert result.next_cursor is None


@respx.mock
async def test_query_thread_replies_non_list_page_no_crash(connector):
    """A corrupt ``messages`` field must fall back to an empty page, not a bare string."""
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "messages": "not-a-list"},
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="thread_replies",
            filters={"channel": "C12345", "thread_ts": "1.2"},
        )
    )
    assert not result.records
    assert result.next_cursor is None


@respx.mock
async def test_query_scheduled_messages_non_list_page_no_crash(connector):
    """A corrupt ``scheduled_messages`` field must fall back to an empty page, not a bare string."""
    respx.get("https://slack.com/api/chat.scheduledMessages.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "scheduled_messages": "not-a-list"}),
    )
    result = await connector.query(ConnectorQuery(resource="scheduled_messages", limit=10))
    assert not result.records
    assert result.next_cursor is None


@respx.mock
async def test_query_messages_non_list_page_no_crash(connector):
    """A corrupt ``messages`` field must fall back to an empty page, not a bare string."""
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": "not-a-list"}),
    )
    result = await connector.query(ConnectorQuery(resource="messages", filters={"channel": "C12345"}))
    assert not result.records
    assert result.next_cursor is None


@respx.mock
async def test_query_channels_api_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_authed"}),
    )
    query = ConnectorQuery(resource="channels")
    with pytest.raises(ValueError, match="not_authed"):
        await connector.query(query)


@respx.mock
async def test_query_channels_http_error(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    query = ConnectorQuery(resource="channels")
    with pytest.raises(ValueError, match="Slack API HTTP 403"):
        await connector.query(query)


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
    assert not result.records


@respx.mock
async def test_query_messages_missing_channel(connector):
    query = ConnectorQuery(resource="messages")
    with pytest.raises(ValueError, match="requires 'channel' filter"):
        await connector.query(query)


@respx.mock
async def test_query_messages_api_error(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    query = ConnectorQuery(resource="messages", filters={"channel": "C99999"})
    with pytest.raises(ValueError, match="channel_not_found"):
        await connector.query(query)


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
    query = ConnectorQuery(resource="users")
    with pytest.raises(ValueError, match="token_revoked"):
        await connector.query(query)


@respx.mock
async def test_query_users_http_error(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    query = ConnectorQuery(resource="users")
    with pytest.raises(ValueError, match="Slack API HTTP 403"):
        await connector.query(query)


# -- query: unsupported resource --


async def test_query_unsupported_resource(connector):
    query = ConnectorQuery(resource="unknown")
    with pytest.raises(ValueError, match="Unsupported Slack resource"):
        await connector.query(query)


# -- write: unsupported resource --


async def test_write_unsupported_resource(connector):
    payload = ConnectorPayload(resource="file", data={})
    with pytest.raises(ValueError, match="Unsupported Slack write resource"):
        await connector.write(payload)


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
    payload = ConnectorPayload(resource="message", data={"text": "Hello"})
    with pytest.raises(ValueError, match="Missing 'channel' in message payload"):
        await connector.write(payload)


@respx.mock
async def test_write_message_api_error(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "too_many_attachments"}),
    )
    payload = ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"})
    with pytest.raises(ValueError, match="too_many_attachments"):
        await connector.write(payload)


@respx.mock
async def test_write_message_http_error(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(500, text="Server Error"),
    )
    payload = ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"})
    with pytest.raises(ValueError, match="Slack API HTTP 500"):
        await connector.write(payload)


# -- rate limiting (old-style direct raises via ValueError) --


@respx.mock
async def test_query_channels_rate_limited(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    query = ConnectorQuery(resource="channels")
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.query(query)


@respx.mock
async def test_query_users_rate_limited(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    query = ConnectorQuery(resource="users")
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.query(query)


@respx.mock
async def test_query_messages_rate_limited(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    query = ConnectorQuery(resource="messages", filters={"channel": "C12345"})
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.query(query)


@respx.mock
async def test_write_message_rate_limited(connector):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    payload = ConnectorPayload(resource="message", data={"channel": "C12345", "text": "Hello"})
    with pytest.raises(ValueError, match="Slack API HTTP"):
        await connector.write(payload)


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


# -- retry/backoff for 5xx --


@respx.mock
async def test_503_retry_then_succeed(connector):
    route = respx.get("https://slack.com/api/conversations.list")
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(200, json={"ok": True, "channels": [{"id": "C001", "name": "retried-after-503"}]}),
    ]
    result = await connector.query(ConnectorQuery(resource="channels"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "retried-after-503"
    assert route.call_count == 2


@respx.mock
async def test_503_retry_exhausted(connector):
    route = respx.get("https://slack.com/api/conversations.list")
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(503, text="Service Unavailable"),
    ]
    with pytest.raises(ValueError, match="Slack API HTTP 503"):
        await connector.query(ConnectorQuery(resource="channels"))
    assert route.call_count == 4


@respx.mock
async def test_502_retry_then_succeed(connector):
    route = respx.get("https://slack.com/api/conversations.list")
    route.side_effect = [
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, json={"ok": True, "channels": [{"id": "C001", "name": "retried-after-502"}]}),
    ]
    result = await connector.query(ConnectorQuery(resource="channels"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "retried-after-502"
    assert route.call_count == 2


@respx.mock
async def test_504_retry_then_succeed(connector):
    route = respx.get("https://slack.com/api/conversations.list")
    route.side_effect = [
        httpx.Response(504, text="Gateway Timeout"),
        httpx.Response(200, json={"ok": True, "channels": [{"id": "C001", "name": "retried-after-504"}]}),
    ]
    result = await connector.query(ConnectorQuery(resource="channels"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "retried-after-504"
    assert route.call_count == 2


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


# -- health_check: bot-in-channel verification --


@respx.mock
async def test_health_check_bot_not_in_channel(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "user_id": "U001"}),
    )
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "channels": []}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Bot is not in any channel" in result.detail


@respx.mock
async def test_health_check_membership_check_network_error(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "user_id": "U001"}),
    )
    respx.get("https://slack.com/api/conversations.list").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail


@respx.mock
async def test_health_check_membership_check_api_error(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "user_id": "U001"}),
    )
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Channel membership check failed" in result.detail


# -- domain-specific exception types --


def test_domain_exception_types_subclass_value_error():
    assert issubclass(SlackError, ValueError)
    assert issubclass(SlackAPIError, SlackError)
    assert issubclass(SlackRateLimitError, SlackAPIError)
    assert issubclass(SlackAuthError, SlackAPIError)
    assert issubclass(SlackNetworkError, SlackError)


@respx.mock
async def test_rate_limit_error_type(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text=""),
    )
    with pytest.raises(SlackRateLimitError, match="Slack API HTTP 429"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_auth_error_type_401(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(401, text="invalid_auth"),
    )
    with pytest.raises(SlackAuthError, match="Slack API HTTP 401"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_auth_error_type_403(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(SlackAuthError, match="Slack API HTTP 403"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_network_error_type_http_500(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    with pytest.raises(SlackNetworkError, match="Slack API HTTP 500"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_network_error_type_timeout(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        side_effect=httpx.TimeoutException("Timed out"),
    )
    with pytest.raises(SlackNetworkError, match="Slack API timeout"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_api_error_type_ok_false(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_authed"}),
    )
    with pytest.raises(SlackAPIError, match="not_authed"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_api_error_type_invalid_json(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    with pytest.raises(SlackAPIError, match="invalid JSON"):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


@respx.mock
async def test_verify_scopes_failure_is_auth_error(connector):
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
    )
    with pytest.raises(SlackAuthError, match="Token validation failed"):
        await connector.verify_scopes()


@respx.mock
async def test_ok_false_errors_are_apis_errors(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
    )
    with pytest.raises(SlackAPIError):
        await connector.query(ConnectorQuery(resource="channels", limit=10))


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
    assert not result.next_cursor


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
async def test_channel_members_corrupt_cursor(connector):
    """A corrupt/hostile response placing a non-dict in ``response_metadata``
    (or a non-string in ``next_cursor``) must not crash pagination — the cursor
    falls back to ``None`` instead of leaking into the next request."""
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "members": ["U001"], "response_metadata": ["garbage"]},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
    )
    assert result.next_cursor is None

    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "members": ["U001"], "response_metadata": {"next_cursor": {"page": 2}}},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
    )
    assert result.next_cursor is None


@respx.mock
async def test_channel_members_api_error(connector):
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"}),
    )
    with pytest.raises(ValueError, match="not_in_channel"):
        await connector.query(
            ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
        )


@respx.mock
async def test_channel_members_non_dict_response_metadata_does_not_crash(connector):
    """A non-dict response_metadata must not crash cursor parsing."""
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "members": ["U001"], "response_metadata": ["corrupt"]},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
    )
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_channel_members_non_string_cursor_not_emitted(connector):
    """A corrupt numeric next_cursor must not be emitted as a pagination cursor."""
    respx.get("https://slack.com/api/conversations.members").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "members": ["U001"], "response_metadata": {"next_cursor": 123}},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="channel_members", filters={"channel": "C001"}),
    )
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_thread_replies_non_dict_response_metadata_does_not_crash(connector):
    """A non-dict response_metadata must not crash cursor parsing on thread replies."""
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "messages": [], "response_metadata": {"next_cursor": {"page": 2}}},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="thread_replies", filters={"channel": "C001", "thread_ts": "123456.000001"}),
    )
    assert not result.records
    assert result.next_cursor is None


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
async def test_thread_replies_corrupt_cursor(connector):
    """A corrupt/hostile response placing a non-dict in ``response_metadata``
    (or a non-string in ``next_cursor``) must not crash pagination — the cursor
    falls back to ``None`` instead of leaking into the next request."""
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "messages": [], "response_metadata": "garbage"},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="thread_replies", filters={"channel": "C001", "thread_ts": "123456.000001"}),
    )
    assert result.next_cursor is None

    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "messages": [], "response_metadata": {"next_cursor": False}},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="thread_replies", filters={"channel": "C001", "thread_ts": "123456.000001"}),
    )
    assert result.next_cursor is None


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


# -- query: user_presence --


@respx.mock
async def test_user_presence(connector):
    respx.get("https://slack.com/api/users.getPresence").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "user": "U001", "presence": "active", "online": True},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="user_presence", filters={"user": "U001"}),
    )
    assert len(result.records) == 1
    assert result.records[0]["presence"] == "active"
    assert result.records[0]["online"] is True


@respx.mock
async def test_user_presence_missing_user(connector):
    with pytest.raises(ValueError, match="requires 'user' filter"):
        await connector.query(ConnectorQuery(resource="user_presence"))


@respx.mock
async def test_user_presence_api_error(connector):
    respx.get("https://slack.com/api/users.getPresence").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "user_not_found"}),
    )
    with pytest.raises(ValueError, match="user_not_found"):
        await connector.query(
            ConnectorQuery(resource="user_presence", filters={"user": "U99999"}),
        )


# -- query: user_profile --


@respx.mock
async def test_user_profile(connector):
    respx.get("https://slack.com/api/users.profile.get").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "profile": {"first_name": "Alice", "last_name": "Smith", "email": "alice@example.com"},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="user_profile", filters={"user": "U001"}),
    )
    assert len(result.records) == 1
    assert result.records[0]["profile"]["first_name"] == "Alice"


@respx.mock
async def test_user_profile_with_include_labels(connector):
    respx.get("https://slack.com/api/users.profile.get").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "profile": {"display_name": "Alice", "skype": "alice123"}},
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="user_profile",
            filters={"user": "U001", "include_labels": True},
        ),
    )
    assert result.records[0]["profile"]["skype"] == "alice123"
    assert respx.calls.last.request.url.params.get("include_labels") == "true"


@respx.mock
async def test_user_profile_missing_user(connector):
    with pytest.raises(ValueError, match="requires 'user' filter"):
        await connector.query(ConnectorQuery(resource="user_profile"))


@respx.mock
async def test_user_profile_api_error(connector):
    respx.get("https://slack.com/api/users.profile.get").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "user_not_found"}),
    )
    with pytest.raises(ValueError, match="user_not_found"):
        await connector.query(
            ConnectorQuery(resource="user_profile", filters={"user": "U99999"}),
        )


# -- query: user_lookup --


@respx.mock
async def test_user_lookup_by_email(connector):
    respx.get("https://slack.com/api/users.lookupByEmail").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "user": {
                    "id": "U001",
                    "name": "alice",
                    "profile": {"email": "alice@example.com"},
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="user_lookup", filters={"email": "alice@example.com"}),
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "U001"


@respx.mock
async def test_user_lookup_missing_email(connector):
    with pytest.raises(ValueError, match="requires 'email' filter"):
        await connector.query(ConnectorQuery(resource="user_lookup"))


@respx.mock
async def test_user_lookup_api_error(connector):
    respx.get("https://slack.com/api/users.lookupByEmail").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "users_not_found"}),
    )
    with pytest.raises(ValueError, match="users_not_found"):
        await connector.query(
            ConnectorQuery(resource="user_lookup", filters={"email": "nobody@example.com"}),
        )


# -- write: ephemeral_message --


@respx.mock
async def test_write_ephemeral_message(connector):
    respx.post("https://slack.com/api/chat.postEphemeral").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "message_ts": "888666", "channel": "C001"},
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="ephemeral_message",
            data={"channel": "C001", "user": "U001", "text": "Only you can see this"},
        ),
    )
    assert result["message_ts"] == "888666"
    assert result["channel"] == "C001"


@respx.mock
async def test_write_ephemeral_message_missing_user(connector):
    with pytest.raises(ValueError, match="Missing 'user' in ephemeral_message"):
        await connector.write(
            ConnectorPayload(resource="ephemeral_message", data={"channel": "C001", "text": "Hello"}),
        )


@respx.mock
async def test_write_ephemeral_message_api_error(connector):
    respx.post("https://slack.com/api/chat.postEphemeral").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "user_not_in_channel"}),
    )
    with pytest.raises(ValueError, match="user_not_in_channel"):
        await connector.write(
            ConnectorPayload(
                resource="ephemeral_message",
                data={"channel": "C001", "user": "U99999", "text": "Hello"},
            ),
        )


# -- write: message_update --


@respx.mock
async def test_write_message_update(connector):
    respx.post("https://slack.com/api/chat.update").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "ts": "1405895017.000506", "channel": "C001"},
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="message_update",
            data={"channel": "C001", "ts": "1405895017.000506", "text": "Updated text"},
        ),
    )
    assert result["ts"] == "1405895017.000506"
    assert result["channel"] == "C001"


@respx.mock
async def test_write_message_update_missing_ts(connector):
    with pytest.raises(ValueError, match="Missing 'ts' in message_update"):
        await connector.write(
            ConnectorPayload(resource="message_update", data={"channel": "C001", "text": "Hello"}),
        )


@respx.mock
async def test_write_message_update_api_error(connector):
    respx.post("https://slack.com/api/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "message_not_found"}),
    )
    with pytest.raises(ValueError, match="message_not_found"):
        await connector.write(
            ConnectorPayload(
                resource="message_update",
                data={"channel": "C001", "ts": "111.111", "text": "Hello"},
            ),
        )


# -- write: message_delete --


@respx.mock
async def test_write_message_delete(connector):
    respx.post("https://slack.com/api/chat.delete").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "1405895017.000506", "channel": "C001"}),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="message_delete",
            data={"channel": "C001", "ts": "1405895017.000506"},
        ),
    )
    assert result["ts"] == "1405895017.000506"
    assert result["channel"] == "C001"


@respx.mock
async def test_write_message_delete_missing_ts(connector):
    with pytest.raises(ValueError, match="Missing 'ts' in message_delete"):
        await connector.write(
            ConnectorPayload(resource="message_delete", data={"channel": "C001"}),
        )


@respx.mock
async def test_write_message_delete_api_error(connector):
    respx.post("https://slack.com/api/chat.delete").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "message_not_found"}),
    )
    with pytest.raises(ValueError, match="message_not_found"):
        await connector.write(
            ConnectorPayload(
                resource="message_delete",
                data={"channel": "C001", "ts": "111.111"},
            ),
        )


# -- write: channel_join / channel_leave --


@respx.mock
async def test_write_channel_join(connector):
    respx.post("https://slack.com/api/conversations.join").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "channel": {"id": "C001", "name": "general", "is_member": True}},
        ),
    )
    result = await connector.write(
        ConnectorPayload(resource="channel_join", data={"channel": "C001"}),
    )
    assert result["channel"]["id"] == "C001"
    assert result["channel"]["is_member"] is True


@respx.mock
async def test_write_channel_join_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in channel_join"):
        await connector.write(ConnectorPayload(resource="channel_join", data={}))


@respx.mock
async def test_write_channel_join_api_error(connector):
    respx.post("https://slack.com/api/conversations.join").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "method_not_supported_for_channel_type"}),
    )
    with pytest.raises(ValueError, match="method_not_supported"):
        await connector.write(
            ConnectorPayload(resource="channel_join", data={"channel": "C001"}),
        )


@respx.mock
async def test_write_channel_leave(connector):
    respx.post("https://slack.com/api/conversations.leave").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": "C001"}),
    )
    result = await connector.write(
        ConnectorPayload(resource="channel_leave", data={"channel": "C001"}),
    )
    assert result["channel"] == "C001"


@respx.mock
async def test_write_channel_leave_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in channel_leave"):
        await connector.write(ConnectorPayload(resource="channel_leave", data={}))


# -- write: channel_archive / channel_unarchive --


@respx.mock
async def test_write_channel_archive(connector):
    respx.post("https://slack.com/api/conversations.archive").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": "C001"}),
    )
    result = await connector.write(
        ConnectorPayload(resource="channel_archive", data={"channel": "C001"}),
    )
    assert result["channel"] == "C001"


@respx.mock
async def test_write_channel_archive_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in channel_archive"):
        await connector.write(ConnectorPayload(resource="channel_archive", data={}))


@respx.mock
async def test_write_channel_archive_api_error(connector):
    respx.post("https://slack.com/api/conversations.archive").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "already_archived"}),
    )
    with pytest.raises(ValueError, match="already_archived"):
        await connector.write(
            ConnectorPayload(resource="channel_archive", data={"channel": "C001"}),
        )


@respx.mock
async def test_write_channel_unarchive(connector):
    respx.post("https://slack.com/api/conversations.unarchive").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": "C001"}),
    )
    result = await connector.write(
        ConnectorPayload(resource="channel_unarchive", data={"channel": "C001"}),
    )
    assert result["channel"] == "C001"


@respx.mock
async def test_write_channel_unarchive_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in channel_unarchive"):
        await connector.write(ConnectorPayload(resource="channel_unarchive", data={}))


@respx.mock
async def test_write_channel_unarchive_api_error(connector):
    respx.post("https://slack.com/api/conversations.unarchive").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_archived"}),
    )
    with pytest.raises(ValueError, match="not_archived"):
        await connector.write(
            ConnectorPayload(resource="channel_unarchive", data={"channel": "C001"}),
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
def test_parse_retry_after_valid():
    resp = httpx.Response(429, headers={"Retry-After": "12.5"})
    assert _parse_retry_after(resp) == 12.5


def test_parse_retry_after_missing():
    resp = httpx.Response(200)
    assert _parse_retry_after(resp) is None


def test_parse_retry_after_invalid():
    resp = httpx.Response(429, headers={"Retry-After": "not-a-number"})
    assert _parse_retry_after(resp) is None


# -- _safe_int coercion edge cases --


def test_safe_int_non_finite_float_returns_default():
    """inf/nan floats must not crash pagination (int(inf) raises OverflowError)."""
    assert _safe_int(float("inf"), 7) == 7
    assert _safe_int(float("-inf"), 7) == 7
    assert _safe_int(float("nan"), 7) == 7


def test_safe_int_rejects_bool_and_wrong_types():
    """Booleans and non-numeric types fall back to default (True == 1 is a footgun)."""
    assert _safe_int(True, 7) == 7
    assert _safe_int(False, 7) == 7
    assert _safe_int(None, 7) == 7
    assert _safe_int([1], 7) == 7
    assert _safe_int({}, 7) == 7


def test_safe_int_rejects_unparseable_strings():
    """Garbage strings (incl. 'inf'/'nan') fall back to default."""
    assert _safe_int("not-a-number", 7) == 7
    assert _safe_int("inf", 7) == 7
    assert _safe_int("nan", 7) == 7


def test_safe_int_coerces_valid_values():
    """Numeric strings, ints, and finite floats coerce to int."""
    assert _safe_int("42", 7) == 42
    assert _safe_int(42, 7) == 42
    assert _safe_int(42.9, 7) == 42
    assert _safe_int(-3, 7) == -3
    assert _safe_int(0, 7) == 0


# -- query: message_search (search.messages) --


@respx.mock
async def test_query_message_search(connector):
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": {
                    "matches": [
                        {"ts": "123456", "text": "Hello world", "user": "U001", "channel": {"id": "C001"}},
                        {"ts": "123457", "text": "World of agents", "user": "U002", "channel": {"id": "C002"}},
                    ],
                    "paging": {"count": 100, "total": 2, "page": 1, "pages": 1},
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="message_search", filters={"query": "world"}, limit=10),
    )
    assert len(result.records) == 2
    assert result.records[0]["text"] == "Hello world"
    assert result.next_cursor is None


@respx.mock
async def test_query_message_search_multi_page(connector):
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": {
                    "matches": [{"ts": "123456", "text": "match", "user": "U001"}],
                    "paging": {"count": 100, "total": 150, "page": 1, "pages": 2},
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="message_search", filters={"query": "match"}),
    )
    assert result.next_cursor == "2"
    assert respx.calls.last.request.url.params.get("count") == "100"


@respx.mock
async def test_query_message_search_non_finite_paging_does_not_crash(connector):
    """A corrupt 'page: 1e999' (json parses to inf) must not crash pagination."""
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(
            200,
            text=(
                '{"ok": true, "messages": {"matches": [{"ts": "123456", "text": "match", "user": "U001"}],'
                ' "paging": {"count": 100, "total": 1, "page": 1e999, "pages": 1}}}'
            ),
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="message_search", filters={"query": "match"}),
    )
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_message_search_garbage_paging_does_not_crash(connector):
    """Non-numeric paging values fall back to page 1/1 and disable pagination."""
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": {
                    "matches": [{"ts": "123456", "text": "match", "user": "U001"}],
                    "paging": {"count": 100, "total": 1, "page": "abc", "pages": []},
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="message_search", filters={"query": "match"}),
    )
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_message_search_with_cursor_and_sort(connector):
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": {
                    "matches": [{"ts": "123458", "text": "next page", "user": "U002"}],
                    "paging": {"count": 100, "total": 250, "page": 2, "pages": 3},
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="message_search",
            filters={"query": "match", "sort": "timestamp"},
            cursor="2",
        ),
    )
    assert len(result.records) == 1
    assert result.next_cursor == "3"
    params = respx.calls.last.request.url.params
    assert params.get("page") == "2"
    assert params.get("sort") == "timestamp"


@respx.mock
async def test_query_message_search_clamps_count_to_max(connector):
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": {
                    "matches": [{"ts": "123459", "text": "match", "user": "U001"}],
                    "paging": {"count": 100, "total": 1, "page": 1, "pages": 1},
                },
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="message_search", filters={"query": "match"}, limit=500),
    )
    assert len(result.records) == 1
    assert respx.calls.last.request.url.params.get("count") == "100"


@respx.mock
async def test_query_message_search_missing_query(connector):
    with pytest.raises(ValueError, match="requires 'query' filter"):
        await connector.query(ConnectorQuery(resource="message_search"))


@respx.mock
async def test_query_message_search_invalid_cursor(connector):
    with pytest.raises(ValueError, match="cursor must be a numeric page"):
        await connector.query(
            ConnectorQuery(resource="message_search", filters={"query": "match"}, cursor="abc"),
        )


@respx.mock
async def test_query_message_search_api_error(connector):
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_search"}),
    )
    with pytest.raises(ValueError, match="invalid_search"):
        await connector.query(
            ConnectorQuery(resource="message_search", filters={"query": "match"}),
        )


@respx.mock
async def test_query_message_search_http_error(connector):
    respx.get("https://slack.com/api/search.messages").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 403"):
        await connector.query(
            ConnectorQuery(resource="message_search", filters={"query": "match"}),
        )


# -- query: messages cursor pagination + types filter --


@respx.mock
async def test_query_messages_forwards_cursor(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "999", "text": "older page", "user": "U001"}],
                "response_metadata": {"next_cursor": "page3"},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="messages", filters={"channel": "C001"}, cursor="page2"),
    )
    assert result.next_cursor == "page3"
    assert respx.calls.last.request.url.params.get("cursor") == "page2"


@respx.mock
async def test_query_messages_types_filter(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []}),
    )
    result = await connector.query(
        ConnectorQuery(resource="messages", filters={"channel": "C001", "types": "messages,joins"}),
    )
    assert not result.records
    assert respx.calls.last.request.url.params.get("types") == "messages,joins"


@respx.mock
async def test_query_thread_replies_forwards_cursor(connector):
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "999", "text": "older reply", "user": "U001"}],
                "response_metadata": {"next_cursor": "next"},
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="thread_replies",
            filters={"channel": "C001", "thread_ts": "123.000"},
            cursor="prev",
        ),
    )
    assert result.next_cursor == "next"
    assert respx.calls.last.request.url.params.get("cursor") == "prev"


# -- write: schedule_message (chat.scheduleMessage) --


@respx.mock
async def test_write_schedule_message(connector):
    respx.post("https://slack.com/api/chat.scheduleMessage").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "channel": "C001", "post_at": "1610118217", "scheduled_message_id": "Q1234"},
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="schedule_message",
            data={"channel": "C001", "post_at": 1610118217, "text": "Scheduled hello"},
        ),
    )
    assert result["scheduled_message_id"] == "Q1234"
    body = respx.calls.last.request.content
    assert b"1610118217" in body


@respx.mock
async def test_write_schedule_message_missing_post_at(connector):
    with pytest.raises(ValueError, match="Missing 'post_at' in schedule_message"):
        await connector.write(
            ConnectorPayload(resource="schedule_message", data={"channel": "C001", "text": "Hello"}),
        )


@respx.mock
async def test_write_schedule_message_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in schedule_message"):
        await connector.write(
            ConnectorPayload(resource="schedule_message", data={"post_at": 1610118217}),
        )


@respx.mock
async def test_write_schedule_message_api_error(connector):
    respx.post("https://slack.com/api/chat.scheduleMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_post_at"}),
    )
    with pytest.raises(ValueError, match="invalid_post_at"):
        await connector.write(
            ConnectorPayload(
                resource="schedule_message",
                data={"channel": "C001", "post_at": "not-a-timestamp"},
            ),
        )


@respx.mock
async def test_write_schedule_message_http_error(connector):
    respx.post("https://slack.com/api/chat.scheduleMessage").mock(
        return_value=httpx.Response(500, text="Server Error"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 500"):
        await connector.write(
            ConnectorPayload(
                resource="schedule_message",
                data={"channel": "C001", "post_at": 1610118217},
            ),
        )


# -- write: file_upload (files.upload) --


@respx.mock
async def test_write_file_upload_content(connector):
    respx.post("https://slack.com/api/files.upload").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "file": {"id": "F1234", "name": "notes.txt", "permalink": "https://.../notes.txt"},
            },
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file_upload",
            data={"filename": "notes.txt", "content": "hello from modulo", "channels": "C001"},
        ),
    )
    assert result["file"]["id"] == "F1234"
    request = respx.calls.last.request
    assert request.url.params.get("filename") is None  # filename travels in multipart, not query


@respx.mock
async def test_write_file_upload_bytes(connector):
    respx.post("https://slack.com/api/files.upload").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "file": {"id": "F5678", "name": "report.bin", "permalink": "https://.../report.bin"},
            },
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file_upload",
            data={
                "filename": "report.bin",
                "file": b"\x00\x01\x02binary",
                "channels": "C002",
                "initial_comment": "see report",
            },
        ),
    )
    assert result["file"]["id"] == "F5678"


@respx.mock
async def test_write_file_upload_missing_filename(connector):
    with pytest.raises(ValueError, match="Missing 'filename' in file_upload"):
        await connector.write(
            ConnectorPayload(resource="file_upload", data={"content": "no name"}),
        )


@respx.mock
async def test_write_file_upload_missing_content(connector):
    with pytest.raises(ValueError, match="requires 'content' or 'file'"):
        await connector.write(
            ConnectorPayload(resource="file_upload", data={"filename": "notes.txt"}),
        )


@respx.mock
async def test_write_file_upload_both_content_and_file(connector):
    with pytest.raises(ValueError, match="exactly one of 'content' or 'file'"):
        await connector.write(
            ConnectorPayload(
                resource="file_upload",
                data={"filename": "notes.txt", "content": "text", "file": b"bytes"},
            ),
        )


@respx.mock
async def test_write_file_upload_api_error(connector):
    respx.post("https://slack.com/api/files.upload").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_file"}),
    )
    with pytest.raises(ValueError, match="invalid_file"):
        await connector.write(
            ConnectorPayload(
                resource="file_upload",
                data={"filename": "notes.txt", "content": "hello"},
            ),
        )


@respx.mock
async def test_write_file_upload_http_error(connector):
    respx.post("https://slack.com/api/files.upload").mock(
        return_value=httpx.Response(413, text="Payload Too Large"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 413"):
        await connector.write(
            ConnectorPayload(
                resource="file_upload",
                data={"filename": "notes.txt", "content": "hello"},
            ),
        )


# -- query: scheduled_messages (chat.scheduledMessages.list) --


@respx.mock
async def test_query_scheduled_messages(connector):
    scheduled = [
        {"id": "Q1234", "channel_id": "C001", "post_at": 1610118217, "text": "Morning standup"},
        {"id": "Q1235", "channel_id": "C002", "post_at": 1610118300, "text": "Daily report"},
    ]
    respx.get("https://slack.com/api/chat.scheduledMessages.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "scheduled_messages": scheduled}),
    )
    result = await connector.query(ConnectorQuery(resource="scheduled_messages", limit=10))
    assert len(result.records) == 2
    assert result.records[0]["id"] == "Q1234"
    assert result.next_cursor is None


@respx.mock
async def test_query_scheduled_messages_with_channel_filter(connector):
    respx.get("https://slack.com/api/chat.scheduledMessages.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "scheduled_messages": []}),
    )
    result = await connector.query(
        ConnectorQuery(resource="scheduled_messages", filters={"channel": "C001"}, limit=10),
    )
    assert not result.records
    assert respx.calls.last.request.url.params["channel"] == "C001"


@respx.mock
async def test_query_scheduled_messages_with_cursor(connector):
    respx.get("https://slack.com/api/chat.scheduledMessages.list").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "scheduled_messages": [{"id": "Q1236", "channel_id": "C001", "post_at": 1610118400}],
                "response_metadata": {"next_cursor": "page2"},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="scheduled_messages", cursor="page1"))
    assert len(result.records) == 1
    assert result.next_cursor == "page2"
    assert respx.calls.last.request.url.params["cursor"] == "page1"


@respx.mock
async def test_query_scheduled_messages_api_error(connector):
    respx.get("https://slack.com/api/chat.scheduledMessages.list").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
    )
    with pytest.raises(ValueError, match="missing_scope"):
        await connector.query(ConnectorQuery(resource="scheduled_messages"))


@respx.mock
async def test_query_scheduled_messages_http_error(connector):
    respx.get("https://slack.com/api/chat.scheduledMessages.list").mock(
        return_value=httpx.Response(500, text="Server Error"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 500"):
        await connector.query(ConnectorQuery(resource="scheduled_messages"))


# -- write: scheduled_message_delete (chat.deleteScheduledMessage) --


@respx.mock
async def test_write_scheduled_message_delete(connector):
    respx.post("https://slack.com/api/chat.deleteScheduledMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="scheduled_message_delete",
            data={"channel": "C001", "scheduled_message_id": "Q1234"},
        ),
    )
    assert result["ok"] is True
    body = respx.calls.last.request.content
    assert b"Q1234" in body


@respx.mock
async def test_write_scheduled_message_delete_missing_channel(connector):
    with pytest.raises(ValueError, match="Missing 'channel' in scheduled_message_delete"):
        await connector.write(
            ConnectorPayload(
                resource="scheduled_message_delete",
                data={"scheduled_message_id": "Q1234"},
            ),
        )


@respx.mock
async def test_write_scheduled_message_delete_missing_id(connector):
    with pytest.raises(ValueError, match="Missing 'scheduled_message_id' in scheduled_message_delete"):
        await connector.write(
            ConnectorPayload(resource="scheduled_message_delete", data={"channel": "C001"}),
        )


@respx.mock
async def test_write_scheduled_message_delete_api_error(connector):
    respx.post("https://slack.com/api/chat.deleteScheduledMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_scheduled_message_id"}),
    )
    with pytest.raises(ValueError, match="invalid_scheduled_message_id"):
        await connector.write(
            ConnectorPayload(
                resource="scheduled_message_delete",
                data={"channel": "C001", "scheduled_message_id": "Q1234"},
            ),
        )


@respx.mock
async def test_write_scheduled_message_delete_http_error(connector):
    respx.post("https://slack.com/api/chat.deleteScheduledMessage").mock(
        return_value=httpx.Response(500, text="Server Error"),
    )
    with pytest.raises(ValueError, match="Slack API HTTP 500"):
        await connector.write(
            ConnectorPayload(
                resource="scheduled_message_delete",
                data={"channel": "C001", "scheduled_message_id": "Q1234"},
            ),
        )
