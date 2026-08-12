"""Structured error hierarchy tests for JiraConnector.

Covers the machine-parseable error codes and the invalid/expired-token vs
insufficient-permission vs rate-limit vs network distinction that lets callers
branch on Jira failures without parsing human-readable messages. This closes
the gap where an expired token and an invalid instance URL were previously
indistinguishable (both surfaced as a generic ``ValueError``).
"""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.jira import (
    JiraAPIError,
    JiraAuthError,
    JiraConnector,
    JiraError,
    JiraNetworkError,
    JiraNotFoundError,
    JiraRateLimitError,
    _error_for_status,
)

_INSTANCE = "test-domain.atlassian.net"
_BASE = f"https://{_INSTANCE}/rest/api/3"
EMAIL = "user@example.com"
API_TOKEN = "jira_api_token"


@pytest.fixture
def connector():
    return JiraConnector(
        instance=_INSTANCE,
        creds={"email": EMAIL, "api_token": API_TOKEN},
    )


# ---------------------------------------------------------------------------
# Error class hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    """All Jira errors derive from JiraError and remain ValueError-compatible."""
    assert issubclass(JiraError, ValueError)
    assert issubclass(JiraAPIError, JiraError)
    assert issubclass(JiraRateLimitError, JiraAPIError)
    assert issubclass(JiraAuthError, JiraAPIError)
    assert issubclass(JiraNotFoundError, JiraAPIError)
    assert issubclass(JiraNetworkError, JiraError)
    assert not issubclass(JiraNetworkError, JiraAPIError)


def test_default_error_codes():
    """Each error type exposes a stable machine-parseable default error_code."""
    assert JiraError("x").error_code == "jira_error"
    assert JiraAPIError("x").error_code == "api_error"
    assert JiraRateLimitError("x").error_code == "rate_limited"
    assert JiraAuthError("x").error_code == "authentication_failed"
    assert JiraNotFoundError("x").error_code == "not_found"
    assert JiraNetworkError("x").error_code == "network_error"


def test_error_holds_status_code():
    """Typed errors record the originating HTTP status for programmatic use."""
    err = JiraAuthError("x", status_code=401, error_code="invalid_token")
    assert err.status_code == 401
    assert err.error_code == "invalid_token"
    assert JiraNetworkError("x").status_code is None


def test_error_for_status_mapping():
    """_error_for_status maps each status to the correct type + code."""
    assert isinstance(_error_for_status(429, "x"), JiraRateLimitError)
    assert isinstance(_error_for_status(401, "x"), JiraAuthError)
    assert isinstance(_error_for_status(403, "x"), JiraAuthError)
    assert isinstance(_error_for_status(404, "x"), JiraNotFoundError)
    assert isinstance(_error_for_status(500, "x"), JiraAPIError)
    assert isinstance(_error_for_status(422, "x"), JiraAPIError)

    assert _error_for_status(401, "x").error_code == "invalid_token"
    assert _error_for_status(403, "x").error_code == "forbidden"
    assert _error_for_status(429, "x").error_code == "rate_limited"
    assert _error_for_status(404, "x").error_code == "not_found"
    assert _error_for_status(500, "x").error_code == "api_error"


# ---------------------------------------------------------------------------
# _call_api raises typed errors on query/write paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_401_raises_auth_error(connector):
    """An expired/invalid token surfaces as JiraAuthError with invalid_token."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(JiraAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "invalid_token"
    assert excinfo.value.status_code == 401


@respx.mock
async def test_query_403_raises_forbidden(connector):
    """A permission denial surfaces as JiraAuthError with forbidden."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(JiraAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "forbidden"
    assert excinfo.value.status_code == 403


@respx.mock
async def test_query_404_raises_not_found(connector):
    """A missing resource surfaces as JiraNotFoundError with not_found."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(JiraNotFoundError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "not_found"
    assert excinfo.value.status_code == 404


@respx.mock
async def test_query_500_raises_api_error(connector):
    """A generic 5xx surfaces as JiraAPIError with api_error."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(500, text="Server Error"))
    with pytest.raises(JiraAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "api_error"
    assert not isinstance(excinfo.value, JiraAuthError)


@respx.mock
async def test_query_exhausted_429_raises_rate_limit_error(connector, monkeypatch):
    """Exhausted 429 (after retries) surfaces as JiraRateLimitError."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.jira.asyncio.sleep", no_sleep)
    route = respx.get(f"{_BASE}/issue/PROJ-123")
    route.mock(return_value=httpx.Response(429, text="Rate limited"))
    with pytest.raises(JiraRateLimitError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "rate_limited"
    assert excinfo.value.status_code == 429
    assert route.call_count == 4


@respx.mock
async def test_query_timeout_raises_network_error(connector, monkeypatch):
    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.jira.asyncio.sleep", no_sleep)
    """Timeout surfaces as JiraNetworkError with network_timeout."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(side_effect=httpx.TimeoutException("Timed out"))
    with pytest.raises(JiraNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "network_timeout"


@respx.mock
async def test_query_connect_error_raises_network_error(connector, monkeypatch):
    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.jira.asyncio.sleep", no_sleep)
    """Connection failure (e.g. unreachable instance URL) surfaces as JiraNetworkError."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(JiraNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "network_connection"


@respx.mock
async def test_query_304_raises_api_error_not_modified(connector):
    """304 surfaces as JiraAPIError with the not_modified code."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(304))
    with pytest.raises(JiraAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "not_modified"
    assert excinfo.value.status_code == 304


@respx.mock
async def test_invalid_json_raises_api_error(connector):
    """Unparseable JSON surfaces as JiraAPIError with invalid_response."""
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(JiraAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert excinfo.value.error_code == "invalid_response"


# ---------------------------------------------------------------------------
# health_check distinguishes failure modes
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_invalid_token(connector):
    """An expired/invalid token is reported distinctly from other failures."""
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid or expired API token" in result.detail
    assert "HTTP 401" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    """A 403 is reported as insufficient permission, not a token problem."""
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "permission denied" in result.detail
    assert "HTTP 403" in result.detail


@respx.mock
async def test_health_check_rate_limited(connector, monkeypatch):
    """Rate-limit exhaustion is reported distinctly with the rate_limited code."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.jira.asyncio.sleep", no_sleep)
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(429, text="Rate limited"))
    result = await connector.health_check()
    assert result.ok is False
    assert "rate limit exhausted" in result.detail
    assert "code: rate_limited" in result.detail


@respx.mock
async def test_health_check_network_error(connector, monkeypatch):
    """A connection failure (e.g. unreachable/invalid instance URL) is distinct from a 401."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.jira.asyncio.sleep", no_sleep)
    respx.get(f"{_BASE}/myself").mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail
    assert "code: network_connection" in result.detail


@respx.mock
async def test_health_check_ok(connector):
    """A successful /myself probe still returns the display name."""
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(200, json={"displayName": "Alice"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"
