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
# Rate-limit budget awareness — X-RateLimit-Reset drives the 429 wait
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_429_uses_rate_limit_reset(connector, monkeypatch):
    """X-RateLimit-Reset (epoch) should drive the retry delay on 429."""

    # Freeze time so the reset window cannot expire mid-request (the
    # connector's own processing time would otherwise push
    # reset_epoch - time.time() <= 0 and fall back to backoff).
    fake_now = 1_000_000.0
    monkeypatch.setattr("modulo.connectors.github.time.time", lambda: fake_now)
    reset_epoch = fake_now + 5.0  # 5s reset window, comfortably in the future
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_epoch)},
                text="Rate limit exceeded",
            ),
            httpx.Response(200, json=[{"id": 1}]),
        ],
    )
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_query_429_rate_limit_headers_in_error(connector):
    """Rate-limit quota headers should surface in the final 429 error detail."""
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                headers={
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1754000000",
                },
                text="Rate limit",
            )
            for _ in range(4)
        ],
    )
    with pytest.raises(ValueError, match="GitHub API HTTP 429") as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert route.call_count == 4
    assert "X-RateLimit-Reset=1754000000" in str(excinfo.value)
    assert "X-RateLimit-Remaining=0" in str(excinfo.value)


def test_retry_delay_429_reset_window_not_capped():
    """A quota reset window longer than _MAX_DELAY must not be capped."""
    import time

    reset_epoch = time.time() + 45.0
    response = httpx.Response(429, headers={"X-RateLimit-Reset": str(reset_epoch)})
    delay = GitHubConnector._retry_delay(response, attempt=0)
    assert 44.0 < delay <= 45.0


def test_retry_delay_retry_after_and_backoff_capped():
    """Retry-After and exponential backoff remain capped at _MAX_DELAY."""
    long_retry_after = httpx.Response(429, headers={"Retry-After": "120"})
    assert GitHubConnector._retry_delay(long_retry_after, attempt=0) == 30.0
    far_reset = httpx.Response(503)
    assert GitHubConnector._retry_delay(far_reset, attempt=5) == 30.0


def test_retry_delay_reset_only_honoured_on_429():
    """X-RateLimit-Reset must only drive the wait on 429, not other statuses.

    GitHub sends X-RateLimit-Reset on every response, so on 502/503/504 it must
    not replace the capped exponential backoff.
    """
    import time

    reset = str(int(time.time()) + 60)
    for status in (502, 503, 504):
        response = httpx.Response(status, headers={"X-RateLimit-Reset": reset})
        assert GitHubConnector._retry_delay(response, attempt=5) == 30.0
    response = httpx.Response(429, headers={"X-RateLimit-Reset": reset})
    assert GitHubConnector._retry_delay(response, attempt=0) > 30.0


def test_parse_rate_limit_reset_missing_or_invalid():
    """X-RateLimit-Reset parsing handles absent/invalid values."""
    import time

    from modulo.connectors.github import _parse_rate_limit_reset

    assert _parse_rate_limit_reset(httpx.Response(200)) is None
    assert _parse_rate_limit_reset(httpx.Response(200, headers={"X-RateLimit-Reset": "not-a-number"})) is None
    past = httpx.Response(200, headers={"X-RateLimit-Reset": str(time.time() - 10)})
    assert _parse_rate_limit_reset(past) is None
    future = httpx.Response(200, headers={"X-RateLimit-Reset": str(time.time() + 10)})
    delay = _parse_rate_limit_reset(future)
    assert delay is not None and 0 < delay <= 10.0
