"""Outbound HTTP client for the hosted community library (FAR-363).

Fail-open client: every public method returns ``None`` on any failure
(network error, non-2xx, SSRF rejection, bad signature, hash mismatch) —
never raises. The community library is strictly optional and must never
block the product.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

from modulo.core.library_sync.manifest import verify_manifest
from modulo.core.ssrf import pinned_async_client

__all__ = ["LibraryClient"]

_log = logging.getLogger(__name__)


class LibraryClient:
    """Lazy httpx client mirroring the Notifier pattern (single shared client,
    explicit timeouts, never-raises contract)."""

    def __init__(
        self,
        endpoint: str,
        root_public_key_pem: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._root_public_key_pem = root_public_key_pem
        self._timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=10.0, pool=10.0)
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._http_client_lock:
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = await pinned_async_client(self._endpoint, timeout=self._timeout)
        return self._http_client

    async def close(self) -> None:
        client: httpx.AsyncClient | None = None
        async with self._http_client_lock:
            if self._http_client is not None and not self._http_client.is_closed:
                client = self._http_client
                self._http_client = None
        if client is not None:
            await client.aclose()

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    async def _get_validated(self, path: str) -> httpx.Response | None:
        """GET ``path`` through the request-time pinned SSRF boundary."""
        url = self._url(path)
        try:
            client = await self._get_client()
            resp = await client.get(url)
        except ValueError as exc:
            _log.warning("library_sync.client.ssrf_rejected", extra={"url": url, "reason": str(exc)})
            return None
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            _log.warning("library_sync.client.fetch_failed", extra={"url": url, "reason": str(exc)})
            return None
        if not resp.is_success:
            _log.warning("library_sync.client.http_error", extra={"url": url, "status": resp.status_code})
            return None
        return resp

    async def fetch_manifest(self) -> dict[str, Any] | None:
        """GET ``/v1/manifest``, verify the Ed25519 signature, return the manifest.

        Returns ``None`` on any failure (never raises, fail-open).
        """
        resp = await self._get_validated("/v1/manifest")
        if resp is None:
            return None
        try:
            manifest = resp.json()
        except ValueError as exc:
            _log.warning("library_sync.client.manifest_parse_failed", extra={"reason": str(exc)})
            return None
        if not isinstance(manifest, dict):
            _log.warning("library_sync.client.manifest_not_object")
            return None
        if not verify_manifest(manifest, self._root_public_key_pem):
            _log.warning("library_sync.client.manifest_bad_signature")
            return None
        return manifest

    async def fetch_blob(self, sha256: str) -> bytes | None:
        """GET ``/v1/blobs/{sha256}`` and verify the content hash matches.

        Returns ``None`` on any failure or hash mismatch (never raises).
        """
        resp = await self._get_validated(f"/v1/blobs/{sha256}")
        if resp is None:
            return None
        content = resp.content
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256.lower():
            _log.warning(
                "library_sync.client.blob_hash_mismatch",
                extra={"expected": sha256, "actual": actual},
            )
            return None
        return content
