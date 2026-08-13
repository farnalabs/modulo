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
    extract_rate_limit_metadata,
    format_rate_limit_detail,
    parse_rate_limit_reset,
    parse_retry_after,
)

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
    assert format_rate_limit_detail(httpx.Response(429), RATE_LIMIT_HEADERS) == ""


def test_format_rate_limit_detail_empty_value_is_omitted() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Limit": ""})
    assert format_rate_limit_detail(response, RATE_LIMIT_HEADERS) == ""


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
