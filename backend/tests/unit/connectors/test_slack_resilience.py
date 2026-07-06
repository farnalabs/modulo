"""Resilience tests for SlackConnector — edge cases and error differentiation."""

import httpx
import pytest
import respx

from modulo.connectors.base import (
    ConnectorQuery,
    ConnectorResult,
)
from modulo.connectors.slack import SlackConnector

BOT_TOKEN = "xoxb-test-token"


@pytest.fixture()
def connector():
    return SlackConnector(bot_token=BOT_TOKEN)


# -- health check: network errors during verify_scopes are correctly labelled --


@respx.mock
async def test_health_check_verify_scopes_connection_error(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail


@respx.mock
async def test_health_check_verify_scopes_timeout(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        side_effect=httpx.TimeoutException("Timed out"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail


@respx.mock
async def test_health_check_verify_scopes_http_error(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(500, text="Internal"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail


# -- empty responses (edge cases) --


@respx.mock
async def test_empty_channel_list(connector):
    respx.get("https://slack.com/api/conversations.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "channels": []}),
    )
    result: ConnectorResult = await connector.query(
        ConnectorQuery(resource="channels", limit=10),
    )
    assert result.records == []
    assert result.next_cursor is None


@respx.mock
async def test_empty_message_history(connector):
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": []}),
    )
    result: ConnectorResult = await connector.query(
        ConnectorQuery(resource="messages", filters={"channel": "C12345"}, limit=10),
    )
    assert result.records == []
    assert result.next_cursor is None


@respx.mock
async def test_empty_user_list(connector):
    respx.get("https://slack.com/api/users.list").mock(
        return_value=httpx.Response(200, json={"ok": True, "members": []}),
    )
    result: ConnectorResult = await connector.query(
        ConnectorQuery(resource="users", limit=10),
    )
    assert result.records == []
    assert result.next_cursor is None


# -- api.test returns ok:false with no error field --


@respx.mock
async def test_health_check_ok_false_no_error_field(connector):
    respx.get("https://slack.com/api/api.test").mock(
        return_value=httpx.Response(200, json={"ok": False}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert result.detail == "unknown"


# -- connector type constant --


def test_connector_type(connector):
    from modulo.connectors.base import ConnectorType

    assert connector.connector_type == ConnectorType.SLACK
