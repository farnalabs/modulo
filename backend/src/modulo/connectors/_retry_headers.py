"""Shared HTTP retry-header parsing for connector clients.

The connector modules (GitHub, GitLab, Jira, Linear, Slack) each implement the
same ``Retry-After`` / rate-limit-reset parsing with per-provider header-name
differences. Keeping the logic in one place avoids drift between the copies
while letting each connector pass its own rate-limit header names.

FAR-410 promotes the retryable-status intersection into a single shared
constant and adds the REST retry semantics (response-code x effect matrix,
``Retry-After`` HTTP-date parsing, split-budget cancellation classification and
per-attempt timeout sizing) that the generic REST connector (FAR-401) composes
with the executor's ``asyncio.wait_for`` budget.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum

import httpx

# Single source of truth for HTTP statuses a connector retries. This is the
# INTERSECTION of the historical per-connector values (GitHub/GitLab/Jira/Slack
# retried 429, 502, 503, 504; Linear additionally retried 500). It is NOT
# widened to all 5xx — that would silently change GitHub/Linear/Jira/Slack
# retry behaviour and risk CI regressions; "5xx retryable" is encoded as a
# REST-only clause (see :func:`rest_retry_decision`). Connectors that
# historically retried additional statuses (Linear's 500) keep that behaviour
# via an explicit augmentation rather than silently gaining it here.
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

# Default per-connector retry budget when a connector does not declare its own.
DEFAULT_MAX_RETRIES = 3

# Upper bound on a ``Retry-After`` delay accepted from an untrusted upstream.
# A malicious/broken response with a huge (or future-dated) ``Retry-After``
# would otherwise schedule an effectively-infinite retry sleep. Any larger
# response is clamped to this value.
MAX_RETRY_AFTER_SECONDS = 30.0

# HTTP statuses that indicate the write was already processed or cannot be
# retried blind: escalating (rather than retrying) prevents double-execution.
_NON_RETRYABLE_WRITE_ESCALATION = frozenset({409, 412, 422})

# Retry/backoff budget shared by the connector clients so their exponential
# backoff, cap, and retryable-status logic cannot drift apart. Connectors
# configure only their own rate-limit header names and feature flags.
MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0


def backoff_delay(attempt: int) -> float:
    """Exponential backoff delay for a retry attempt, capped at ``MAX_DELAY``."""
    return min(BASE_DELAY * (1 << attempt), MAX_DELAY)


def should_retry_status(status_code: int, attempt: int) -> bool:
    """Whether a retryable HTTP status still has retry budget remaining."""
    return status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES


def should_retry_network(attempt: int) -> bool:
    """Whether a transport-level failure may be retried on this attempt."""
    return attempt < MAX_RETRIES


def parse_retry_after(response: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header (seconds) into a retry delay.

    Returns ``None`` when the header is absent or not parseable as a finite
    float (including ``nan``/``inf``, which would poison downstream retry
    delays).
    """
    value = response.headers.get("Retry-After")
    if value:
        try:
            parsed = float(value)
        except (ValueError, TypeError):
            pass
        else:
            if math.isfinite(parsed):
                return parsed
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
    if not math.isfinite(reset_epoch):
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


# ── Retry-After HTTP-date form (RFC 7231 §2.3.3) ─────────────────────────────


def parse_retry_after_http_date(response: httpx.Response) -> float | None:
    """Parse an ``Retry-After`` HTTP-date header into a non-negative delay.

    ``Retry-After`` may carry either delta-seconds (already handled by
    :func:`parse_retry_after`) or an HTTP-date (``Fri, 31 Dec 1999 23:59:59
    GMT``). This handles the HTTP-date form, returning the number of seconds
    until that timestamp (``None`` when the header is absent, unparseable as a
    date, or already in the past).
    """
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delay = (parsed - datetime.now(UTC)).total_seconds()
    return delay if delay > 0 else None


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Best-effort ``Retry-After`` delay, honoring either delta-seconds or HTTP-date.

    Prefers the numeric delta-seconds form (exact), then the HTTP-date form.
    Returns ``None`` when the header is absent, not parseable, or non-positive —
    a zero/negative delta would schedule an immediate/no-op retry that is better
    handled by the caller's blind backoff than by trusting the upstream. The
    returned delay is clamped to :data:`MAX_RETRY_AFTER_SECONDS` so an untrusted
    upstream cannot schedule an effectively-infinite retry sleep.
    """
    delta = parse_retry_after(response)
    if delta is not None:
        if delta <= 0:
            return None
        return min(delta, MAX_RETRY_AFTER_SECONDS)
    date_delay = parse_retry_after_http_date(response)
    if date_delay is not None:
        return min(date_delay, MAX_RETRY_AFTER_SECONDS)
    return None


# ── REST retry semantics: response-code x effect matrix ───────────────────────


class RestRetryDecision(StrEnum):
    """Outcome a connector should take for one HTTP response (FAR-410)."""

    # Retry the request after the computed delay.
    RETRY = "retry"
    # Surface a non-retryable failure (a definitive client error).
    FAIL = "fail"
    # Escalate for human/config resolution — never blind-retry a response
    # indicating the write was already processed or is blocked (409/412/422),
    # or a rate-limit that may have partially applied a keyed write.
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RestRetryContext:
    """Inputs the matrix needs beyond the status code (FAR-410)."""

    is_write: bool = False
    retry_after_present: bool = False
    keyed_write_verified: bool = False


def rest_retry_decision(
    status_code: int,
    *,
    context: RestRetryContext | None = None,
    retryable_statuses: frozenset[int] = RETRYABLE_STATUSES,
) -> RestRetryDecision:
    """Classify one REST response into retry / fail / escalate (FAR-410).

    The matrix (response-code x effect):

    * ``409`` / ``412`` / ``422`` — escalate (non-retryable). These indicate
      the write was already processed (409 conflict), a precondition failed
      (412) or the payload was understood but refused (422); blind-retrying
      risks double-application.
    * ``429`` — retry only for reads, or for a verified-unapplied keyed write
      that carries ``Retry-After``. A write rate-limited with no proof the key
      was not applied escalates rather than retry (never blob-retry a 4xx
      indicating the write may already be processed).
    * ``5xx`` — retry (REST clause). Includes :data:`RETRYABLE_STATUSES`.
    * any other ``4xx`` — fail (definitive client error).
    * anything else — fail.
    """
    ctx = context or RestRetryContext()
    if status_code in _NON_RETRYABLE_WRITE_ESCALATION:
        return RestRetryDecision.ESCALATE
    if status_code == 429:
        if not ctx.is_write:
            return RestRetryDecision.RETRY
        if ctx.retry_after_present and ctx.keyed_write_verified:
            return RestRetryDecision.RETRY
        return RestRetryDecision.ESCALATE
    if status_code in retryable_statuses or status_code >= 500:
        return RestRetryDecision.RETRY
    return RestRetryDecision.FAIL


# ── Split budgets: per-attempt timeout vs node wait_for budget ────────────────


class CancellationPhase(StrEnum):
    """Which phase an attempt was cancelled in (FAR-410 split-budget semantics)."""

    # Cancelled mid-send, before any response: the request may have reached the
    # upstream, so its side-effect state is UNKNOWN (surface, do not hard-fail).
    MID_SEND = "mid_send"
    # Cancelled while backing off between attempts: nothing was sent in this
    # phase, so the outcome is a deterministic FAILED (not UNKNOWN).
    BACKOFF = "backoff"
    # Cancelled after retries were exhausted: the request finished sending but
    # failed — a deterministic FAILED (not UNKNOWN).
    RETRY_EXHAUSTED = "retry_exhausted"


def cancellation_is_unknown(phase: CancellationPhase) -> bool:
    """True iff a per-attempt cancellation yields an UNKNOWN outcome.

    Only a mid-send cancellation is UNKNOWN — the bytes may or may not have
    reached the upstream. A cancellation during backoff or after retry
    exhaustion is a deterministic FAILED, never UNKNOWN.
    """
    return phase is CancellationPhase.MID_SEND


def per_attempt_timeout_seconds(
    node_wait_for_seconds: float,
    *,
    max_retries: int = 0,
    safety_margin_fraction: float = 0.2,
    min_margin_seconds: float = 0.5,
) -> float | None:
    """Size a per-attempt timeout strictly below the node's ``wait_for`` budget.

    The per-attempt timeout must compose INSIDE the executor's
    ``asyncio.wait_for`` budget rather than overrun it: a single attempt may
    consume a full connection timeout, then the budget is already spent. This
    returns a per-attempt budget leaving a safety margin so the node's
    ``wait_for`` never fires first.

    When ``max_retries`` is nonzero, the budget is allocated across ALL attempts
    (the original send plus each retry) so a later retry is not allowed to
    blow the whole node budget mid-send: the allocation divides the
    safely-usable window by ``max_retries + 1``, reserving headroom for the
    backoff/sleep between attempts. A caller must pass the SAME ``max_retries``
    it will actually use, otherwise a retry could fire ``wait_for`` mid-send and
    yield a spurious UNKNOWN (FAR-410).

    Returns ``None`` when the node budget is too small to split safely (the
    caller should then use a single attempt). ``max_retries`` must be >= 0 or a
    ``ValueError`` is raised.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    headroom = max(node_wait_for_seconds * safety_margin_fraction, min_margin_seconds)
    per_attempt = (node_wait_for_seconds - headroom) / (max_retries + 1)
    if per_attempt <= 0 or per_attempt >= node_wait_for_seconds:
        return None
    return per_attempt
