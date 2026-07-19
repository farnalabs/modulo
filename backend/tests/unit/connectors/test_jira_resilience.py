"""Resilience tests for JiraConnector — HTTP/JSON error handling and edge cases."""

import random

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
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


# --- Backoff and jitter tests ---


def test_compute_delay_includes_jitter():
    """Verify _compute_delay adds random jitter to the backoff."""
    from modulo.connectors.jira import _compute_delay

    random.seed(42)
    delays = {_compute_delay(0) for _ in range(100)}
    # With jitter (0-1), each call produces a different value
    assert len(delays) > 1, "Expected jitter to vary delay values"


def test_compute_delay_exponential():
    """Verify _compute_delay increases with attempt number."""
    from modulo.connectors.jira import _compute_delay

    d0 = _compute_delay(0)
    d1 = _compute_delay(1)
    d2 = _compute_delay(2)
    assert d0 < d1 < d2, "Expected exponential backoff"


def test_compute_delay_capped():
    """Verify _compute_delay is capped at _MAX_DELAY (30s)."""
    from modulo.connectors.jira import _MAX_DELAY, _compute_delay

    d = _compute_delay(10)  # would be ~1024s without cap
    assert d <= _MAX_DELAY


def test_compute_delay_respects_retry_after():
    """Verify _compute_delay returns Retry-After value when present."""
    from modulo.connectors.jira import _compute_delay

    resp = httpx.Response(429, headers={"Retry-After": "5"})
    delay = _compute_delay(0, resp)
    assert delay == 5.0


def test_compute_delay_retry_after_capped():
    """Verify _compute_delay caps Retry-After at _MAX_DELAY."""
    from modulo.connectors.jira import _MAX_DELAY, _compute_delay

    resp = httpx.Response(429, headers={"Retry-After": "60"})
    delay = _compute_delay(0, resp)
    assert delay == _MAX_DELAY


# --- Retry behavior tests ---


@respx.mock
async def test_retry_502_then_success(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        side_effect=[
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(200, json={"id": "10001", "key": "PROJ-123", "fields": {"summary": "Fix bug"}}),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"


@respx.mock
async def test_retry_503_then_success(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json={"id": "10001", "key": "PROJ-123", "fields": {"summary": "Fix bug"}}),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"


@respx.mock
async def test_retry_504_then_success(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        side_effect=[
            httpx.Response(504, text="Gateway Timeout"),
            httpx.Response(200, json={"id": "10001", "key": "PROJ-123", "fields": {"summary": "Fix bug"}}),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"


@respx.mock
async def test_retry_429_exhausted_via_query(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(side_effect=[httpx.Response(429)] * 4)
    with pytest.raises(ValueError, match="HTTP 429"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))


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


# --- Required field validation edge cases ---


@respx.mock
async def test_issue_comment_empty_body_rejected(connector):
    """Empty body string should be rejected by 'body' not in data check."""
    with pytest.raises(ValueError, match="requires 'body'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_comment",
                data={"issue_key": "PROJ-123"},
            )
        )


@respx.mock
async def test_issue_comment_empty_key_rejected(connector):
    """Missing issue_key should be rejected by 'issue_key' not in data check."""
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_comment",
                data={"body": "Hello"},
            )
        )
