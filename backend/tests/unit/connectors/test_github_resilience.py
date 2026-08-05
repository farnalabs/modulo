"""Resilience tests for GitHubConnector — HTTP/network errors wrapped as ValueError."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.github import GitHubConnector

TOKEN = "ghp_test_token"


@pytest.fixture()
def connector():
    return GitHubConnector(token=TOKEN)


@respx.mock
async def test_query_repos_429_rate_limit(connector):
    """429 rate limit raises ValueError with status code."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(429, text="Rate limit exceeded"))
    with pytest.raises(ValueError, match="429"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_500_error(connector):
    """500 server error raises ValueError with status code."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))
    with pytest.raises(ValueError, match="500"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_connection_error(connector):
    """Connection error raises ValueError with descriptive message."""
    respx.get("https://api.github.com/user/repos").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(ValueError, match="connection error"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_invalid_json(connector):
    """Invalid JSON response raises ValueError."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(ValueError, match="invalid JSON"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_write_file_429_rate_limit(connector):
    """Write file with 429 raises ValueError with status code."""
    respx.put("https://api.github.com/repos/owner/repo/contents/test.txt").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded")
    )
    with pytest.raises(ValueError, match="429"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"repo": "owner/repo", "path": "test.txt", "content": "data"},
            )
        )


@respx.mock
async def test_health_check_connection_error(connector):
    """Health check returns HealthResult(ok=False) on connection error."""
    respx.get("https://api.github.com/user").mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail.lower()


@respx.mock
async def test_health_check_invalid_json(connector):
    """Health check returns HealthResult(ok=False) on invalid JSON."""
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, text="not-json", headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid JSON" in result.detail


# ---------------------------------------------------------------------------
# Retry/backoff — retryable status codes (429, 502, 503, 504)
# ---------------------------------------------------------------------------


@respx.mock
async def test_retry_on_429_then_success(connector):
    """429 triggers retry and succeeds on second attempt."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit"),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_retry_on_503_then_success(connector):
    """503 triggers retry and succeeds on second attempt."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(503, text="Unavailable"),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_retry_gives_up_after_max_retries(connector):
    """429 retries up to max then raises."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(return_value=httpx.Response(429, text="Rate limit"))
    with pytest.raises(ValueError, match="429"):
        await connector.query(ConnectorQuery(resource="repos"))
    assert route.call_count == 4  # original + 3 retries


@respx.mock
async def test_retry_on_connection_error_then_success(connector):
    """Connection error triggers retry and succeeds on second attempt."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.ConnectError("Connection refused"),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_connection_error_gives_up_after_max_retries(connector):
    """Connection error retries up to max then raises."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(ValueError, match="connection error"):
        await connector.query(ConnectorQuery(resource="repos"))
    assert route.call_count == 4


@respx.mock
async def test_retry_on_timeout_then_success(connector):
    """Timeout triggers retry and succeeds on second attempt."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.TimeoutException("Timeout"),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_retry_respects_retry_after_header(connector):
    """Retry delay uses Retry-After header when present."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit", headers={"Retry-After": "1"}),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Rate-limit budget awareness — X-RateLimit-Reset window wait on 429
# ---------------------------------------------------------------------------


@respx.mock
async def test_retry_waits_for_rate_limit_reset_window(connector):
    """429 with X-RateLimit-Reset waits until the window opens instead of blind backoff."""
    reset_epoch = str(int(__import__("time").time()) + 60)
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit", headers={"X-RateLimit-Reset": reset_epoch}),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_retry_rate_limit_reset_elapsed_falls_back(connector):
    """An already-elapsed X-RateLimit-Reset falls back to backoff (no crash)."""
    reset_epoch = str(int(__import__("time").time()) - 5)
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit", headers={"X-RateLimit-Reset": reset_epoch}),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_exhausted_429_surfaces_quota_headers(connector):
    """Exhausted 429 error detail carries the X-RateLimit-* quota headers."""
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(
            429,
            text="Rate limit exceeded",
            headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1785880000"},
        )
    )
    with pytest.raises(ValueError, match=r"X-RateLimit-Limit=60.*X-RateLimit-Remaining=0"):
        await connector.query(ConnectorQuery(resource="repos"))
