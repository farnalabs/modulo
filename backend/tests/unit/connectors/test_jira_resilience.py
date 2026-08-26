"""Resilience tests for JiraConnector — HTTP/JSON error handling and edge cases."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.jira import JiraConnector

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


# --- Backoff and jitter tests ---


def test_compute_delay_includes_jitter():
    """Verify _compute_delay adds random jitter to the backoff."""
    from modulo.connectors.jira import _compute_delay

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


# --- X-RateLimit-* header inspection tests ---


def test_parse_rate_limit_reset(monkeypatch):
    """X-RateLimit-Reset (epoch seconds) becomes a positive wait delay."""
    from modulo.connectors.jira import _parse_rate_limit_reset

    monkeypatch.setattr("time.time", lambda: 1_000_000)
    resp = httpx.Response(429, headers={"X-RateLimit-Reset": "1000010"})
    delay = _parse_rate_limit_reset(resp)
    assert delay == pytest.approx(10.0)


def test_parse_rate_limit_reset_missing(monkeypatch):
    """No X-RateLimit-Reset header -> None (blind backoff fallback)."""
    from modulo.connectors.jira import _parse_rate_limit_reset

    monkeypatch.setattr("time.time", lambda: 1_000_000)
    assert _parse_rate_limit_reset(httpx.Response(429)) is None


def test_parse_rate_limit_reset_invalid():
    """Non-numeric X-RateLimit-Reset -> None."""
    from modulo.connectors.jira import _parse_rate_limit_reset

    resp = httpx.Response(429, headers={"X-RateLimit-Reset": "not-a-number"})
    assert _parse_rate_limit_reset(resp) is None


def test_parse_rate_limit_reset_in_the_past(monkeypatch):
    """A reset epoch already elapsed -> None (no point waiting)."""
    from modulo.connectors.jira import _parse_rate_limit_reset

    monkeypatch.setattr("time.time", lambda: 1_000_000)
    resp = httpx.Response(429, headers={"X-RateLimit-Reset": "999999"})
    assert _parse_rate_limit_reset(resp) is None


def test_rate_limit_detail_summarises_headers():
    """Quota headers are summarised for error/health detail strings."""
    from modulo.connectors.jira import _rate_limit_detail

    resp = httpx.Response(
        429,
        headers={"X-RateLimit-Limit": "10000", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1754160000"},
    )
    detail = _rate_limit_detail(resp)
    assert "X-RateLimit-Limit=10000" in detail
    assert "X-RateLimit-Remaining=0" in detail
    assert "X-RateLimit-Reset=1754160000" in detail


def test_rate_limit_detail_empty_when_absent():
    """No rate-limit headers -> empty detail string."""
    from modulo.connectors.jira import _rate_limit_detail

    assert not _rate_limit_detail(httpx.Response(429))


def test_rate_limit_metadata_only_present_headers():
    """Only headers actually on the response are surfaced."""
    from modulo.connectors.jira import _rate_limit_metadata

    resp = httpx.Response(200, headers={"X-RateLimit-Remaining": "42"})
    assert _rate_limit_metadata(resp) == {"X-RateLimit-Remaining": "42"}


def test_sleep_delay_uses_rate_limit_reset_on_429(connector, monkeypatch):
    """On 429 with X-RateLimit-Reset, sleep until the quota window resets."""
    import time

    monkeypatch.setattr(time, "time", lambda: 1_000_000)
    resp = httpx.Response(429, headers={"X-RateLimit-Reset": "1000010"})
    delay = connector._sleep_delay(resp, 0)
    assert 9.0 <= delay <= 10.0, "tight jitter should stay within the quota window"


def test_sleep_delay_falls_back_to_backoff_on_429(connector):
    """On 429 without X-RateLimit-Reset, fall back to blind backoff + jitter."""
    resp = httpx.Response(429)
    delay = connector._sleep_delay(resp, 1)
    # attempt 1 -> _BASE_DELAY * 2**1 = 2s plus full jitter in [0, 1)
    assert 2.0 <= delay < 3.0


@respx.mock
async def test_retry_429_then_success_with_reset_header(connector, monkeypatch):
    """A 429 carrying X-RateLimit-Reset retries using the quota wait, then succeeds."""
    import asyncio
    import time

    monkeypatch.setattr(time, "time", lambda: 1_000_000)
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Reset": "1000010"}),
            httpx.Response(200, json={"id": "10001", "key": "PROJ-123"}),
        ]
    )
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"
    assert sleeps, "expected a retry sleep before success"
    assert sleeps[0] > 8.0, "429 retry should wait near the X-RateLimit-Reset window"


@respx.mock
async def test_429_exhausted_includes_quota_detail(connector, monkeypatch):
    """Final 429 error surfaces X-RateLimit-* quota headers in the message."""
    import asyncio

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Limit": "10000", "X-RateLimit-Remaining": "0"}),
            httpx.Response(429, headers={"X-RateLimit-Limit": "10000", "X-RateLimit-Remaining": "0"}),
            httpx.Response(429, headers={"X-RateLimit-Limit": "10000", "X-RateLimit-Remaining": "0"}),
            httpx.Response(429, headers={"X-RateLimit-Limit": "10000", "X-RateLimit-Remaining": "0"}),
        ]
    )
    with pytest.raises(ValueError, match="HTTP 429") as exc:
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert "quota: X-RateLimit-Limit=10000; X-RateLimit-Remaining=0" in str(exc.value)


# --- _safe_int coercion edge cases ---


def test_safe_int_non_finite_float_returns_default():
    """inf/nan floats must not crash pagination (int(inf) raises OverflowError)."""
    from modulo.connectors.jira import _safe_int

    assert _safe_int(float("inf"), 7) == 7
    assert _safe_int(float("-inf"), 7) == 7
    assert _safe_int(float("nan"), 7) == 7


def test_safe_int_rejects_bool_and_wrong_types():
    """Booleans and non-numeric types fall back to default (True == 1 is a footgun)."""
    from modulo.connectors.jira import _safe_int

    assert _safe_int(True, 7) == 7
    assert _safe_int(False, 7) == 7
    assert _safe_int(None, 7) == 7
    assert _safe_int([1], 7) == 7


def test_safe_int_rejects_unparseable_strings():
    """Garbage strings (incl. 'inf'/'nan') fall back to default."""
    from modulo.connectors.jira import _safe_int

    assert _safe_int("not-a-number", 7) == 7
    assert _safe_int("inf", 7) == 7
    assert _safe_int("nan", 7) == 7


def test_safe_int_coerces_valid_values():
    """Numeric strings, ints, and finite floats coerce to int."""
    from modulo.connectors.jira import _safe_int

    assert _safe_int("42", 7) == 42
    assert _safe_int(42, 7) == 42
    assert _safe_int(42.9, 7) == 42
    assert _safe_int(-3, 7) == -3
    assert _safe_int(0, 7) == 0


@respx.mock
async def test_search_with_corrupt_total_does_not_crash(connector):
    """A corrupt 'total: 1e999' (json parses to inf) falls back to issue count."""
    respx.post(f"{_BASE}/search").mock(
        return_value=httpx.Response(
            200,
            text='{"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}], "total": 1e999, "startAt": 0, "maxResults": 2}',
        )
    )
    result = await connector.query(ConnectorQuery(resource="search", filters={"jql": "project = PROJ"}))
    assert [r["key"] for r in result.records] == ["PROJ-1", "PROJ-2"]
    assert result.total == 2
    assert result.next_cursor is None
