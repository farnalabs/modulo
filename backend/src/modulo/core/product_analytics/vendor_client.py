"""Shared outbound HTTP helper for the product-analytics dump.

Provides retry/timeout/backoff/429 logic (extracted from the Notifier
pattern) and HMAC signing.  Every exception is caught inside the
caller — the client itself never re-raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

import httpx

_log = logging.getLogger(__name__)

# Retry configuration — mirrors Notifier RETRY_DELAYS.
MAX_ATTEMPTS = 4  # 1 initial + 3 retries
RETRY_DELAYS = [1.0, 5.0, 30.0]

# Per-request deadline (design doc §8).
REQUEST_TIMEOUT = 30.0


def sign_outbound_batch(secret: str, payload: bytes, timestamp: float, sequence: int) -> str:
    """Compute HMAC-SHA256 for the outbound vendor batch protocol.

    Returns the hex digest string (no ``sha256=`` prefix).

    Wire format (vendor batch protocol)
    -----------------------------------
    The message fed into HMAC-SHA256 is::

        payload + f"{timestamp}:{sequence}"

    This is the canonical signer for batches posted to the metrics vendor via
    :meth:`VendorClient.post_batch`.  It is **NOT** interchangeable with the
    rotation-request signer in
    :mod:`modulo.core.product_analytics.hmac_verify`: that module signs with a
    ``|<timestamp:.6f>|<sequence>`` format under a fixed-precision cross-SDK
    contract.  The two protocols intentionally use different wire formats — never
    reuse this helper to sign or verify rotation requests, and never reuse the
    hmac_verify helper for outbound batches.
    """
    message = payload + f"{timestamp}:{sequence}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class VendorClient:
    """HTTP client for posting metrics batches to the vendor endpoint.

    Handles retry with exponential backoff, 429 Retry-After, and
    per-request timeouts.  All exceptions are caught and surfaced as
    return values — never re-raised.
    """

    def __init__(self, endpoint_url: str, instance_secret: str) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._instance_secret = instance_secret
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=30.0))
        return self._http_client

    def _standard_delay(self, attempt: int) -> float:
        """Backoff delay for the given (1-based) attempt."""
        return RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]

    def _delay_for_429(self, resp: httpx.Response, attempt: int) -> float:
        """Retry-After delay for a 429, falling back to the standard backoff."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after is None:
            return self._standard_delay(attempt)
        try:
            return min(float(retry_after), 60.0)
        except (ValueError, TypeError):
            return self._standard_delay(attempt)

    def _describe_error(self, exc: Exception) -> str:
        """Human-readable message for a transport-level failure."""
        if isinstance(exc, TimeoutError):
            return f"Timeout after {REQUEST_TIMEOUT}s"
        return f"RequestError: {exc}"

    def _log_attempt_failure(self, attempt: int, last_error: str) -> None:
        _log.warning(
            "product_analytics.vendor_post_attempt_failed",
            extra={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "last_error": last_error},
        )

    async def _post_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        signature: str,
        payload: bytes,
        timestamp: float,
        sequence: int,
    ) -> httpx.Response:
        """Execute a single POST attempt.

        Transport/timeout errors propagate to the caller, which owns the
        failure message and backoff.  On success the caller inspects the
        returned response.
        """
        return await asyncio.wait_for(
            client.post(
                url,
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Modulo-Signature": signature,
                    "X-Modulo-Timestamp": str(timestamp),
                    "X-Modulo-Sequence": str(sequence),
                    "User-Agent": "Modulo-MetricsDump/1.0",
                },
            ),
            timeout=REQUEST_TIMEOUT,
        )

    async def post_batch(
        self,
        payload: bytes,
        timestamp: float,
        sequence: int,
    ) -> tuple[bool, int | None, str | None]:
        """POST a metrics batch to the vendor.

        Returns ``(success, status_code, error_message)``.
        """
        signature = sign_outbound_batch(self._instance_secret, payload, timestamp, sequence)
        url = f"{self._endpoint_url}/api/v1/batch"

        client = await self._get_client()
        last_error: str | None = None
        response_code: int | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await self._post_once(client, url, signature, payload, timestamp, sequence)
            except (TimeoutError, httpx.RequestError) as exc:
                last_error = self._describe_error(exc)
                response_code = None
            except Exception as exc:
                last_error = f"Unexpected: {exc}"
                response_code = None
            else:
                response_code = resp.status_code
                if resp.is_success:
                    return True, response_code, None
                if resp.status_code == 400:
                    return False, resp.status_code, f"HTTP 400 (terminal): {resp.text[:200]}"
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code == 429:
                    if attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(self._delay_for_429(resp, attempt))
                    continue

            if attempt < MAX_ATTEMPTS:
                self._log_attempt_failure(attempt, last_error)
                await asyncio.sleep(self._standard_delay(attempt))

        return False, response_code, last_error

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None
