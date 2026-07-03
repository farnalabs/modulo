"""Resilience tests for JiraConnector — HTTP/JSON error handling."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery, ConnectorType, HealthResult
from modulo.connectors.jira import JiraConnector

_INSTANCE = "test-domain.atlassian.net"
_BASE = f"https://{_INSTANCE}/rest/api/3"
EMAIL = "user@example.com"
API_TOKEN = "jira_api_token"


@pytest.fixture()
def connector():
    return JiraConnector(
        instance=_INSTANCE,
        creds={"email": EMAIL, "api_token": API_TOKEN},
    )


@respx.mock
async def test_http_429_rate_limit_raises_valueerror(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(429, text="Rate limit exceeded"))
    with pytest.raises(ValueError, match="HTTP 429"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))


@respx.mock
async def test_http_500_raises_valueerror(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(ValueError, match="HTTP 500"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))


@respx.mock
async def test_connection_error_raises_valueerror(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(ValueError, match="connection error"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))


@respx.mock
async def test_invalid_json_response_raises_valueerror(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(ValueError, match="invalid response"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))


@respx.mock
async def test_health_check_connection_error_returns_ok_false(connector):
    respx.get(f"{_BASE}/myself").mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
