"""Structured error hierarchy tests for LinearConnector.

Covers the machine-parseable error codes and the invalid-key vs
insufficient-permission vs rate-limit vs network distinction that lets callers
branch on Linear failures without parsing human-readable messages.
"""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.linear import (
    LinearAPIError,
    LinearAuthError,
    LinearConnector,
    LinearError,
    LinearNetworkError,
    LinearNotFoundError,
    LinearRateLimitError,
    _classify_graphql_error,
    _error_for_status,
)

API_KEY = "lin_api_key_xxxx"
_GRAPHQL = "https://api.linear.app/graphql"


@pytest.fixture
def connector():
    return LinearConnector(api_key=API_KEY)


# ---------------------------------------------------------------------------
# Error class hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    """All Linear errors derive from LinearError and remain ValueError-compatible."""
    assert issubclass(LinearError, ValueError)
    assert issubclass(LinearAPIError, LinearError)
    assert issubclass(LinearRateLimitError, LinearAPIError)
    assert issubclass(LinearAuthError, LinearAPIError)
    assert issubclass(LinearNotFoundError, LinearAPIError)
    assert issubclass(LinearNetworkError, LinearError)
    assert not issubclass(LinearNetworkError, LinearAPIError)


def test_default_error_codes():
    """Each error type exposes a stable machine-parseable default error_code."""
    assert LinearError("x").error_code == "linear_error"
    assert LinearAPIError("x").error_code == "api_error"
    assert LinearRateLimitError("x").error_code == "rate_limited"
    assert LinearAuthError("x").error_code == "authentication_failed"
    assert LinearNotFoundError("x").error_code == "not_found"
    assert LinearNetworkError("x").error_code == "network_error"


def test_error_holds_status_code():
    """Typed errors record the originating HTTP status for programmatic use."""
    err = LinearAuthError("x", status_code=401, error_code="invalid_token")
    assert err.status_code == 401
    assert err.error_code == "invalid_token"
    assert LinearNetworkError("x").status_code is None


def test_error_for_status_mapping():
    """_error_for_status maps each status to the correct type + code."""
    assert isinstance(_error_for_status(429, "x"), LinearRateLimitError)
    assert isinstance(_error_for_status(401, "x"), LinearAuthError)
    assert isinstance(_error_for_status(403, "x"), LinearAuthError)
    assert isinstance(_error_for_status(404, "x"), LinearNotFoundError)
    assert isinstance(_error_for_status(500, "x"), LinearAPIError)
    assert isinstance(_error_for_status(422, "x"), LinearAPIError)

    assert _error_for_status(401, "x").error_code == "invalid_token"
    assert _error_for_status(403, "x").error_code == "forbidden"
    assert _error_for_status(429, "x").error_code == "rate_limited"
    assert _error_for_status(404, "x").error_code == "not_found"
    assert _error_for_status(500, "x").error_code == "api_error"


def test_classify_graphql_error():
    """Linear GraphQL body errors classify auth failures distinctly."""
    assert isinstance(
        _classify_graphql_error(
            [{"message": "Authentication required", "extensions": {"type": "AUTHENTICATION_REQUIRED"}}]
        ),
        LinearAuthError,
    )
    assert (
        _classify_graphql_error(
            [{"message": "Authentication required", "extensions": {"type": "AUTHENTICATION_REQUIRED"}}]
        ).error_code
        == "invalid_token"
    )
    assert isinstance(
        _classify_graphql_error([{"message": "Forbidden", "extensions": {"type": "FORBIDDEN"}}]),
        LinearAuthError,
    )
    assert (
        _classify_graphql_error([{"message": "Forbidden", "extensions": {"type": "FORBIDDEN"}}]).error_code
        == "forbidden"
    )
    generic = _classify_graphql_error([{"message": "Not authenticated"}])
    assert isinstance(generic, LinearAPIError)
    assert not isinstance(generic, LinearAuthError)
    assert generic.error_code == "api_error"


def test_classify_graphql_error_malformed():
    """Malformed GraphQL error payloads never raise and fall back to api_error."""
    assert isinstance(_classify_graphql_error([]), LinearAPIError)
    assert isinstance(_classify_graphql_error("not-a-list"), LinearAPIError)
    assert isinstance(_classify_graphql_error([{"extensions": {"type": 42}}]), LinearAPIError)
    assert isinstance(_classify_graphql_error([{"message": "x", "extensions": "bad"}]), LinearAPIError)


# ---------------------------------------------------------------------------
# _graphql raises typed errors on query/write paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_401_raises_auth_error(connector):
    """An expired/invalid API key surfaces as LinearAuthError with invalid_token."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(LinearAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "invalid_token"
    assert excinfo.value.status_code == 401


@respx.mock
async def test_query_403_raises_forbidden(connector):
    """A permission denial surfaces as LinearAuthError with forbidden."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(LinearAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "forbidden"
    assert excinfo.value.status_code == 403


@respx.mock
async def test_query_404_raises_not_found(connector):
    """A missing resource surfaces as LinearNotFoundError with not_found."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(LinearNotFoundError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "not_found"
    assert excinfo.value.status_code == 404


@respx.mock
async def test_query_500_raises_api_error(connector):
    """A generic 5xx surfaces as LinearAPIError with api_error."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(500, text="Server Error"))
    with pytest.raises(LinearAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "api_error"
    assert not isinstance(excinfo.value, LinearAuthError)


@respx.mock
async def test_query_exhausted_429_raises_rate_limit_error(connector, monkeypatch):
    """Exhausted 429 (after retries) surfaces as LinearRateLimitError."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.linear.asyncio.sleep", no_sleep)
    route = respx.post(_GRAPHQL)
    route.mock(return_value=httpx.Response(429, text="Rate limited"))
    with pytest.raises(LinearRateLimitError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "rate_limited"
    assert excinfo.value.status_code == 429
    assert route.call_count == 4


@respx.mock
async def test_query_timeout_raises_network_error(connector, monkeypatch):
    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.linear.asyncio.sleep", no_sleep)
    """Timeout surfaces as LinearNetworkError with network_timeout."""
    respx.post(_GRAPHQL).mock(side_effect=httpx.TimeoutException("Timed out"))
    with pytest.raises(LinearNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "network_timeout"


@respx.mock
async def test_query_connect_error_raises_network_error(connector, monkeypatch):
    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.linear.asyncio.sleep", no_sleep)
    """Connection failure surfaces as LinearNetworkError with network_connection."""
    respx.post(_GRAPHQL).mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(LinearNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "network_connection"


@respx.mock
async def test_query_protocol_error_raises_network_error(connector, monkeypatch):
    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.linear.asyncio.sleep", no_sleep)
    """Protocol failure surfaces as LinearNetworkError with network_protocol."""
    respx.post(_GRAPHQL).mock(side_effect=httpx.RemoteProtocolError("Server disconnected"))
    with pytest.raises(LinearNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "network_protocol"


@respx.mock
async def test_query_invalid_json_raises_api_error(connector):
    """A malformed response surfaces as LinearAPIError with invalid_response."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(LinearAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "invalid_response"


@respx.mock
async def test_graphql_auth_error_classified(connector):
    """A GraphQL AUTHENTICATION_REQUIRED body error surfaces as invalid_token."""
    respx.post(_GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "errors": [{"message": "Authentication required", "extensions": {"type": "AUTHENTICATION_REQUIRED"}}]
            },
        )
    )
    with pytest.raises(LinearAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "invalid_token"


@respx.mock
async def test_graphql_forbidden_classified(connector):
    """A GraphQL FORBIDDEN body error surfaces as forbidden."""
    respx.post(_GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Forbidden", "extensions": {"type": "FORBIDDEN"}}]},
        )
    )
    with pytest.raises(LinearAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert excinfo.value.error_code == "forbidden"


# ---------------------------------------------------------------------------
# health_check distinguishes failure modes
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_invalid_key_http(connector):
    """HTTP 401 reports an invalid/expired API key explicitly."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid or expired API key" in result.detail
    assert "401" in result.detail
    assert "invalid_token" in result.detail


@respx.mock
async def test_health_check_invalid_key_graphql(connector):
    """A GraphQL AUTHENTICATION_REQUIRED error reports an invalid API key."""
    respx.post(_GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "errors": [{"message": "Authentication required", "extensions": {"type": "AUTHENTICATION_REQUIRED"}}]
            },
        )
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid or expired API key" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    """HTTP 403 reports a key that lacks the required permission."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "permission denied" in result.detail
    assert "forbidden" in result.detail


@respx.mock
async def test_health_check_rate_limited(connector, monkeypatch):
    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.linear.asyncio.sleep", no_sleep)
    """Exhausted 429 reports the rate-limit failure distinctly."""
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(429, text="Rate limited"))
    result = await connector.health_check()
    assert result.ok is False
    assert "rate limit" in result.detail.lower()
    assert "429" in result.detail


@respx.mock
async def test_health_check_network_error(connector):
    """A connection failure reports a network error, not an auth error."""
    respx.post(_GRAPHQL).mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail.lower()
    assert "network_connection" in result.detail
    assert "auth" not in result.detail.lower()


@respx.mock
async def test_health_check_ok(connector):
    """A valid API key passes with the viewer name in detail."""
    respx.post(_GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"viewer": {"id": "u1", "name": "Alice", "email": "alice@example.com"}}},
        )
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"
