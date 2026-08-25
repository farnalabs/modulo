"""Unit tests for the shared connector retry-header helper module.

The helper lives at ``modulo.connectors._retry_headers`` and is consumed by
GitHub, GitLab, Jira, Linear and Slack connectors. These tests cover it
directly (including the detail/metadata formatters that have no other direct
coverage) so the shared contract stays locked down regardless of per-connector
wrappers.
"""

import time

import httpx
import pytest

from modulo.connectors._retry_headers import (
    MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUSES,
    CancellationPhase,
    RestRetryContext,
    RestRetryDecision,
    cancellation_is_unknown,
    extract_rate_limit_metadata,
    format_rate_limit_detail,
    parse_rate_limit_reset,
    parse_retry_after,
    parse_retry_after_http_date,
    per_attempt_timeout_seconds,
    rest_retry_decision,
    retry_after_seconds,
)
from modulo.core.pipeline_engine.error_codes import known_error_codes, map_legacy_code

RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)


# ── parse_retry_after ───────────────────────────────────────────────────────


def test_parse_retry_after_valid_seconds() -> None:
    response = httpx.Response(429, headers={"Retry-After": "12.5"})
    assert parse_retry_after(response) == 12.5


def test_parse_retry_after_integer_seconds() -> None:
    response = httpx.Response(429, headers={"Retry-After": "3"})
    assert parse_retry_after(response) == 3.0


def test_parse_retry_after_zero_is_not_treated_as_absent() -> None:
    response = httpx.Response(429, headers={"Retry-After": "0"})
    assert parse_retry_after(response) == 0.0


def test_parse_retry_after_missing_header() -> None:
    assert parse_retry_after(httpx.Response(429)) is None


def test_parse_retry_after_empty_header() -> None:
    response = httpx.Response(429, headers={"Retry-After": ""})
    assert parse_retry_after(response) is None


def test_parse_retry_after_invalid_value() -> None:
    response = httpx.Response(429, headers={"Retry-After": "soon"})
    assert parse_retry_after(response) is None


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "12.5.5", "  "])
def test_parse_retry_after_non_finite_or_malformed_values(value: str) -> None:
    response = httpx.Response(429, headers={"Retry-After": value})
    assert parse_retry_after(response) is None


# ── parse_rate_limit_reset ──────────────────────────────────────────────────


def test_parse_rate_limit_reset_future_returns_positive_delay() -> None:
    reset = str(int(time.time()) + 60)
    response = httpx.Response(429, headers={"X-RateLimit-Reset": reset})
    delay = parse_rate_limit_reset(response, RATE_LIMIT_HEADERS)
    assert delay is not None
    assert 0 < delay <= 60


def test_parse_rate_limit_reset_past_returns_none() -> None:
    reset = str(int(time.time()) - 60)
    response = httpx.Response(429, headers={"X-RateLimit-Reset": reset})
    assert parse_rate_limit_reset(response, RATE_LIMIT_HEADERS) is None


def test_parse_rate_limit_reset_elapsed_window_returns_none() -> None:
    reset = str(int(time.time()))
    response = httpx.Response(429, headers={"X-RateLimit-Reset": reset})
    assert parse_rate_limit_reset(response, RATE_LIMIT_HEADERS) is None


def test_parse_rate_limit_reset_missing_header() -> None:
    assert parse_rate_limit_reset(httpx.Response(429), RATE_LIMIT_HEADERS) is None


def test_parse_rate_limit_reset_invalid_value() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Reset": "not-a-number"})
    assert parse_rate_limit_reset(response, RATE_LIMIT_HEADERS) is None


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_parse_rate_limit_reset_non_finite_values(value: str) -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Reset": value})
    assert parse_rate_limit_reset(response, RATE_LIMIT_HEADERS) is None


def test_parse_rate_limit_reset_uses_first_present_header() -> None:
    reset = str(int(time.time()) + 60)
    response = httpx.Response(
        429,
        headers={"RateLimit-Reset": "garbage", "X-RateLimit-Reset": reset},
    )
    delay = parse_rate_limit_reset(response, ("X-RateLimit-Reset", "RateLimit-Reset"))
    assert delay is not None
    assert 0 < delay <= 60


def test_parse_rate_limit_reset_falls_back_to_next_header() -> None:
    reset = str(int(time.time()) + 60)
    response = httpx.Response(429, headers={"RateLimit-Reset": reset})
    delay = parse_rate_limit_reset(response, ("X-RateLimit-Reset", "RateLimit-Reset"))
    assert delay is not None
    assert 0 < delay <= 60


# ── format_rate_limit_detail ────────────────────────────────────────────────


def test_format_rate_limit_detail_joins_present_headers() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999"})
    detail = format_rate_limit_detail(response, RATE_LIMIT_HEADERS)
    assert detail == "X-RateLimit-Limit=5000; X-RateLimit-Remaining=4999"


def test_format_rate_limit_detail_omits_absent_headers() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Limit": "5000"})
    detail = format_rate_limit_detail(response, RATE_LIMIT_HEADERS)
    assert detail == "X-RateLimit-Limit=5000"


def test_format_rate_limit_detail_no_headers() -> None:
    assert not format_rate_limit_detail(httpx.Response(429), RATE_LIMIT_HEADERS)


def test_format_rate_limit_detail_empty_value_is_omitted() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Limit": ""})
    assert not format_rate_limit_detail(response, RATE_LIMIT_HEADERS)


# ── extract_rate_limit_metadata ─────────────────────────────────────────────


def test_extract_rate_limit_metadata_only_present_headers() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Limit": "5000", "X-RateLimit-Reset": "12345"})
    meta = extract_rate_limit_metadata(response, RATE_LIMIT_HEADERS)
    assert meta == {"X-RateLimit-Limit": "5000", "X-RateLimit-Reset": "12345"}


def test_extract_rate_limit_metadata_no_headers() -> None:
    assert not extract_rate_limit_metadata(httpx.Response(429), RATE_LIMIT_HEADERS)


def test_extract_rate_limit_metadata_keeps_empty_values() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Limit": ""})
    assert extract_rate_limit_metadata(response, RATE_LIMIT_HEADERS) == {"X-RateLimit-Limit": ""}


# ── RETRYABLE_STATUSES shared intersection ───────────────────────────────────


def test_shared_retryable_statuses_is_the_per_connector_intersection() -> None:
    """The shared set is the INTERSECTION {429, 502, 503, 504} — not all 5xx."""
    assert frozenset({429, 502, 503, 504}) == RETRYABLE_STATUSES
    assert 500 not in RETRYABLE_STATUSES
    assert 501 not in RETRYABLE_STATUSES


# ── parse_retry_after_http_date ──────────────────────────────────────────────


def test_parse_retry_after_http_date_future_returns_positive_delay() -> None:
    now = time.time()
    future = now + 60
    # httpdate is whole-second precision, so round the expected window down.
    fmt = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future))
    response = httpx.Response(429, headers={"Retry-After": fmt})
    delay = parse_retry_after_http_date(response)
    assert delay is not None
    assert 55 < delay <= 61


def test_parse_retry_after_http_date_past_returns_none() -> None:
    past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 600))
    response = httpx.Response(429, headers={"Retry-After": past})
    assert parse_retry_after_http_date(response) is None


def test_parse_retry_after_http_date_missing_or_invalid() -> None:
    assert parse_retry_after_http_date(httpx.Response(429)) is None
    assert parse_retry_after_http_date(httpx.Response(429, headers={"Retry-After": "soon"})) is None


def test_retry_after_seconds_prefers_delta_then_http_date() -> None:
    response = httpx.Response(429, headers={"Retry-After": "7"})
    assert retry_after_seconds(response) == 7.0
    fmt = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 15))
    response = httpx.Response(429, headers={"Retry-After": fmt})
    delay = retry_after_seconds(response)
    assert delay is not None
    assert 10 < delay <= 16


def test_retry_after_seconds_caps_unbounded_delta() -> None:
    """An untrusted upstream must not schedule an effectively-infinite retry sleep."""
    response = httpx.Response(429, headers={"Retry-After": "300"})
    assert retry_after_seconds(response) == MAX_RETRY_AFTER_SECONDS
    response = httpx.Response(429, headers={"Retry-After": "3600"})
    assert retry_after_seconds(response) == MAX_RETRY_AFTER_SECONDS


def test_retry_after_seconds_caps_future_http_date() -> None:
    """A far-future HTTP-date is clamped too, not returned verbatim."""
    fmt = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 86400))
    response = httpx.Response(429, headers={"Retry-After": fmt})
    assert retry_after_seconds(response) == MAX_RETRY_AFTER_SECONDS


def test_retry_after_seconds_rejects_non_positive_delta() -> None:
    """A zero/negative delta is not trusted for scheduling (mirror rate-limit reset)."""
    for value in ("0", "-5"):
        response = httpx.Response(429, headers={"Retry-After": value})
        assert retry_after_seconds(response) is None


# ── rest_retry_decision: response-code x effect matrix ───────────────────────


def test_rest_409_412_422_escalate_never_blind_retry() -> None:
    for status in (409, 412, 422):
        assert rest_retry_decision(status, context=RestRetryContext(is_write=True)) is RestRetryDecision.ESCALATE
        assert rest_retry_decision(status, context=RestRetryContext(is_write=False)) is RestRetryDecision.ESCALATE


def test_rest_429_read_is_retryable() -> None:
    assert rest_retry_decision(429, context=RestRetryContext(is_write=False)) is RestRetryDecision.RETRY


def test_rest_429_write_with_retry_after_but_unverified_escalates() -> None:
    ctx = RestRetryContext(is_write=True, retry_after_present=True, keyed_write_verified=False)
    assert rest_retry_decision(429, context=ctx) is RestRetryDecision.ESCALATE


def test_rest_429_write_verified_unapplied_retries() -> None:
    ctx = RestRetryContext(is_write=True, retry_after_present=True, keyed_write_verified=True)
    assert rest_retry_decision(429, context=ctx) is RestRetryDecision.RETRY


def test_rest_429_write_without_retry_after_escalates() -> None:
    ctx = RestRetryContext(is_write=True, retry_after_present=False)
    assert rest_retry_decision(429, context=ctx) is RestRetryDecision.ESCALATE


def test_rest_5xx_is_retryable_rest_clause() -> None:
    for status in (500, 501, 502, 503, 504, 599):
        assert rest_retry_decision(status) is RestRetryDecision.RETRY


def test_rest_other_4xx_fails() -> None:
    for status in (400, 401, 403, 404, 415):
        assert rest_retry_decision(status) is RestRetryDecision.FAIL


# ── cancellation_is_unknown: split-budget semantics ──────────────────────────


def test_only_mid_send_cancellation_is_unknown() -> None:
    assert cancellation_is_unknown(CancellationPhase.MID_SEND) is True
    assert cancellation_is_unknown(CancellationPhase.BACKOFF) is False
    assert cancellation_is_unknown(CancellationPhase.RETRY_EXHAUSTED) is False


# ── per_attempt_timeout_seconds: per-attempt < node wait_for ─────────────────


def test_per_attempt_timeout_is_strictly_below_node_budget() -> None:
    per_attempt = per_attempt_timeout_seconds(60.0)
    assert per_attempt is not None
    assert 0 < per_attempt < 60.0


def test_per_attempt_timeout_tiny_budget_returns_none() -> None:
    assert per_attempt_timeout_seconds(0.2) is None


def test_per_attempt_timeout_negative_max_retries_raises() -> None:
    with pytest.raises(ValueError):
        per_attempt_timeout_seconds(60.0, max_retries=-1)


def test_per_attempt_timeout_with_retries_is_smaller_than_single() -> None:
    single = per_attempt_timeout_seconds(60.0)
    with_retries = per_attempt_timeout_seconds(60.0, max_retries=3)
    assert single is not None
    assert with_retries is not None
    assert with_retries < single


def test_per_attempt_timeout_retry_aware_allocates_within_budget() -> None:
    """Retry-aware allocation keeps EVERY attempt (incl. later retries) within budget."""
    node_budget = 60.0
    max_retries = 3
    per_attempt = per_attempt_timeout_seconds(node_budget, max_retries=max_retries)
    assert per_attempt is not None
    # The whole attempt sequence plus the safety margin must stay under the budget.
    assert per_attempt * (max_retries + 1) < node_budget
    assert per_attempt > 0
    # The default sizing (max_retries=0) is the unsplit single-attempt budget.
    single = per_attempt_timeout_seconds(node_budget)
    assert single is not None
    assert per_attempt * (max_retries + 1) <= single


# ── connector.side_effect_unknown taxonomy: distinct code, not harness.unknown ──


def test_connector_side_effect_unknown_spellings_map_to_distinct_code() -> None:
    """All raw spellings of the connector write-UNKNOWN code resolve to the
    DISTINCT connector.side_effect_unknown code, never collapsing to
    harness.unknown (which would lose the critical terminal state)."""
    assert map_legacy_code("ConnectorUnknownError") == "connector.side_effect_unknown"
    assert map_legacy_code("connector_unknown") == "connector.side_effect_unknown"
    assert map_legacy_code("connector.unknown") == "connector.side_effect_unknown"
    assert map_legacy_code("connector.side_effect_unknown") == "connector.side_effect_unknown"
    assert "connector.side_effect_unknown" in known_error_codes()
    assert "ConnectorUnknownError" in known_error_codes()
    assert "connector.unknown" in known_error_codes()
    assert map_legacy_code("ConnectorUnknownError") != "harness.unknown"
    assert map_legacy_code("connector_unknown") != "harness.unknown"
