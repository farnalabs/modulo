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


def compute_hmac(secret: str, payload: bytes, timestamp: float, sequence: int) -> str:
    """Compute HMAC-SHA256 over ``payload + timestamp + sequence``.

    Returns the hex digest string (no ``sha256=`` prefix).
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

    async def post_batch(
        self,
        payload: bytes,
        timestamp: float,
        sequence: int,
    ) -> tuple[bool, int | None, str | None]:
        """POST a metrics batch to the vendor.

        Returns ``(success, status_code, error_message)``.
        """
        signature = compute_hmac(self._instance_secret, payload, timestamp, sequence)
        url = f"{self._endpoint_url}/api/v1/batch"

        client = await self._get_client()
        last_error: str | None = None
        response_code: int | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await asyncio.wait_for(
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
                response_code = resp.status_code
                if resp.is_success:
                    return True, response_code, None

                # 400 = terminal (design doc §8) — no retry.
                if resp.status_code == 400:
                    return False, resp.status_code, f"HTTP 400 (terminal): {resp.text[:200]}"

                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"

                # Respect Retry-After on 429.
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = min(float(retry_after), 60.0)
                        except (ValueError, TypeError):
                            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    else:
                        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    if attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(delay)
                    continue

            except TimeoutError:
                last_error = f"Timeout after {REQUEST_TIMEOUT}s"
                response_code = None
            except httpx.RequestError as exc:
                last_error = f"RequestError: {exc}"
                response_code = None
            except Exception as exc:
                last_error = f"Unexpected: {exc}"
                response_code = None

            if attempt < MAX_ATTEMPTS:
                _log.warning(
                    "product_analytics.vendor_post_attempt_failed",
                    extra={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "last_error": last_error},
                )
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)

        return False, response_code, last_error

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None
