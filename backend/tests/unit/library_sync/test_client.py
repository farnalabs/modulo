"""Unit tests for the community-library HTTP client (FAR-363).

Uses ``respx`` to mock the httpx transport and stubs the SSRF validator so no
test ever touches real DNS. The client contract is fail-open: every public
method returns ``None`` on any failure and never raises.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from modulo.core.library_sync.client import LibraryClient
from modulo.core.library_sync.manifest import canonical_manifest_bytes
from modulo.registry.crypto import generate_keypair, sign

ENDPOINT = "https://library.modulo.run"


def _signed_manifest(private_key_pem: str, **overrides: object) -> dict:
    body: dict[str, object] = {
        "schema_version": "1",
        "generated_at": "2026-08-22T00:00:00Z",
        "entries": [],
        "revoked": [],
    }
    body.update(overrides)
    canonical = canonical_manifest_bytes(body)
    return {**body, "signature": {"algorithm": "ed25519", "value": sign(private_key_pem, canonical)}}


@pytest.fixture
def keys() -> tuple[str, str]:
    return generate_keypair()


@pytest.fixture
def client(keys: tuple[str, str]) -> LibraryClient:
    _, public_key = keys
    return LibraryClient(endpoint=ENDPOINT, root_public_key_pem=public_key)


@pytest.fixture(autouse=True)
def _stub_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hit real DNS in unit tests; the URL is treated as allowed."""

    async def _allow(_url: str) -> None:
        return None

    monkeypatch.setattr("modulo.core.library_sync.client.validate_outbound_url_async", _allow)


class TestFetchManifest:
    @respx.mock
    async def test_returns_none_on_http_error(self, client: LibraryClient) -> None:
        respx.get(f"{ENDPOINT}/v1/manifest").mock(return_value=httpx.Response(500))
        assert await client.fetch_manifest() is None

    @respx.mock
    async def test_returns_none_on_network_error(self, client: LibraryClient) -> None:
        respx.get(f"{ENDPOINT}/v1/manifest").mock(side_effect=httpx.ConnectError("connection refused"))
        assert await client.fetch_manifest() is None

    @respx.mock
    async def test_returns_manifest_on_success(self, keys: tuple[str, str], client: LibraryClient) -> None:
        private_key, _ = keys
        manifest = _signed_manifest(private_key, entries=[{"id": "agent-1", "type": "agent"}])
        respx.get(f"{ENDPOINT}/v1/manifest").mock(return_value=httpx.Response(200, json=manifest))
        assert await client.fetch_manifest() == manifest

    @respx.mock
    async def test_rejects_bad_signature(self, client: LibraryClient) -> None:
        other_private, _ = generate_keypair()
        manifest = _signed_manifest(other_private)
        respx.get(f"{ENDPOINT}/v1/manifest").mock(return_value=httpx.Response(200, json=manifest))
        assert await client.fetch_manifest() is None

    @respx.mock
    async def test_returns_none_on_non_object_payload(self, keys: tuple[str, str], client: LibraryClient) -> None:
        private_key, _ = keys
        manifest = _signed_manifest(private_key)
        respx.get(f"{ENDPOINT}/v1/manifest").mock(return_value=httpx.Response(200, json=[manifest]))
        assert await client.fetch_manifest() is None


class TestFetchBlob:
    @respx.mock
    async def test_returns_content_when_hash_matches(self, client: LibraryClient) -> None:
        content = b"<plugin>hello</plugin>"
        sha256 = hashlib.sha256(content).hexdigest()
        respx.get(f"{ENDPOINT}/v1/blobs/{sha256}").mock(return_value=httpx.Response(200, content=content))
        assert await client.fetch_blob(sha256) == content

    @respx.mock
    async def test_returns_none_on_hash_mismatch(self, client: LibraryClient) -> None:
        expected = hashlib.sha256(b"expected").hexdigest()
        respx.get(f"{ENDPOINT}/v1/blobs/{expected}").mock(return_value=httpx.Response(200, content=b"different"))
        assert await client.fetch_blob(expected) is None

    @respx.mock
    async def test_returns_none_on_http_error(self, client: LibraryClient) -> None:
        sha256 = hashlib.sha256(b"x").hexdigest()
        respx.get(f"{ENDPOINT}/v1/blobs/{sha256}").mock(return_value=httpx.Response(404))
        assert await client.fetch_blob(sha256) is None
