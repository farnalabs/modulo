"""Shared HTTP retry-header parsing for connector clients.

The connector modules (GitHub, GitLab, Jira, Linear, Slack) each implement the
same ``Retry-After`` / rate-limit-reset parsing with per-provider header-name
differences. Keeping the logic in one place avoids drift between the copies
while letting each connector pass its own rate-limit header names.
"""

from __future__ import annotations

import time

import httpx


def parse_retry_after(response: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header (seconds) into a retry delay.

    Returns ``None`` when the header is absent or not parseable as a float.
    """
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


def parse_rate_limit_reset(response: httpx.Response, reset_headers: tuple[str, ...]) -> float | None:
    """Parse a rate-limit reset header (epoch seconds) into a retry delay.

    ``reset_headers`` lists candidate header names in preference order
    (GitHub/Jira use ``X-RateLimit-Reset``; GitLab uses ``RateLimit-ResetTime``
    falling back to ``RateLimit-Reset``). When a 429 response includes one, the
    client can wait until the quota window resets instead of guessing with
    blind backoff.
    """
    value: str | None = None
    for header in reset_headers:
        candidate = response.headers.get(header)
        if candidate:
            value = candidate
            break
    if not value:
        return None
    try:
        reset_epoch = float(value)
    except (ValueError, TypeError):
        return None
    delay = reset_epoch - time.time()
    return delay if delay > 0 else None


def format_rate_limit_detail(response: httpx.Response, headers: tuple[str, ...]) -> str:
    """Summarise present rate-limit quota headers into a detail string."""
    parts = [f"{header}={value}" for header in headers if (value := response.headers.get(header))]
    return "; ".join(parts)


def extract_rate_limit_metadata(response: httpx.Response, headers: tuple[str, ...]) -> dict[str, str | None]:
    """Extract present rate-limit quota headers into a metadata dict.

    Only headers present on the response are included, so an empty dict simply
    means no rate-limit reporting.
    """
    return {name: response.headers.get(name) for name in headers if name in response.headers}
