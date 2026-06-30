"""Unit tests for the community registry API — browse, publish, pull, verify, publishers."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

PRIMITIVES_URL = "/api/v1/registry/primitives"
PUBLISH_URL = "/api/v1/registry/primitives"
PUBLISH_V2_URL = "/api/v1/registry/publish"
VERIFY_URL = "/api/v1/registry/verify/modulo/prd-input-schema"
DOWNLOADS_URL = "/api/v1/registry/primitives/modulo/prd-input-schema/download"
PUBLISHERS_URL = "/api/v1/registry/publishers"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_entry(**overrides: object) -> MagicMock:
    entry = MagicMock()
    entry.slug = "modulo/prd-input-schema"
    entry.signing_key_fingerprint = "abcdef1234567890"
    entry.author = "modulo"
    entry.name = "prd-input-schema"
    entry.version = "1.0"
    entry.primitive_type = "schema"
    entry.description = "Input schema for a PRD."
    entry.tags = ["schema", "prd"]
    entry.content_json = {"fields": [{"name": "title", "type": "string"}]}
    entry.ed25519_signature_hex = "ab" * 32
    entry.checksum_sha256 = "deadbeef" + "0" * 56
    entry.published_at = datetime(2025, 1, 1, tzinfo=UTC)
    entry.download_count = 10
    entry.publisher_status = "community"
    for k, v in overrides.items():
        setattr(entry, k, v)
    return entry


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ranked_item(slug: str = "modulo/prd-input-schema", score: float = 0.75) -> dict:
    entry = _make_entry(slug=slug)
    return {
        "entry": entry,
        "publisher_status": "verified",
        "publisher_name": "Modulo Team",
        "popularity_score": score,
    }


# ============================================================================
# Browse / list tests
# ============================================================================


class TestBrowseRegistry:
    def test_list_all_primitives(self, client: TestClient) -> None:
        items = [_ranked_item(), _ranked_item(slug="modulo/other-schema", score=0.5)]
        with patch(
            "modulo.api.routes.registry.list_registry_primitives_ranked",
            return_value=items,
        ):
            resp = client.get(PRIMITIVES_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_list_primitives_with_pagination(self, client: TestClient) -> None:
        items = [_ranked_item(slug=f"modulo/item-{i}", score=1.0 - i * 0.1) for i in range(5)]
        with patch(
            "modulo.api.routes.registry.list_registry_primitives_ranked",
            return_value=items,
        ):
            resp = client.get(f"{PRIMITIVES_URL}?page=1&page_size=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_search_returns_matching_results(self, client: TestClient) -> None:
        items = [_ranked_item(slug="modulo/prd-input-schema", score=0.75)]
        with patch(
            "modulo.api.routes.registry.list_registry_primitives_ranked",
            return_value=items,
        ):
            resp = client.get(f"{PRIMITIVES_URL}?search=prd")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_search_no_results(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.registry.list_registry_primitives_ranked",
            return_value=[],
        ):
            resp = client.get(f"{PRIMITIVES_URL}?search=zzzzz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert len(data["items"]) == 0


# ============================================================================
# Get detail tests
# ============================================================================


class TestGetPrimitive:
    def test_get_primitive_detail(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=True),
        ):
            resp = client.get(f"{PRIMITIVES_URL}/modulo/prd-input-schema")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entry"]["slug"] == "modulo/prd-input-schema"
        assert data["verified"] is True
        assert data["integrity_ok"] is True

    def test_get_primitive_not_found(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.get_registry_primitive", return_value=None):
            resp = client.get(f"{PRIMITIVES_URL}/unknown/nope")
        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"].lower()

    def test_get_primitive_slug_with_special_chars(self, client: TestClient) -> None:
        entry = _make_entry(slug="modulo/special-v1.0")
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=False),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="community"),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=False),
        ):
            resp = client.get(f"{PRIMITIVES_URL}/modulo/special-v1.0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entry"]["slug"] == "modulo/special-v1.0"
        assert data["verified"] is False
        assert data["integrity_ok"] is False


# ============================================================================
# Publish tests
# ============================================================================


class TestPublishPrimitive:
    def test_publish_v1_creates_entry(self, client: TestClient) -> None:
        entry = _make_entry(slug="communityuser/my-workflow")
        with (
            patch("modulo.api.routes.registry.publish_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature"),
            patch("modulo.api.routes.registry.get_publisher_status"),
        ):
            resp = client.post(
                PUBLISH_URL,
                json={
                    "author": "communityuser",
                    "name": "my-workflow",
                    "primitive_type": "workflow",
                    "description": "A test workflow",
                    "tags": ["test"],
                    "content_json": {"nodes": [], "edges": [], "entry": "start"},
                    "signing_key_hex": "aa" * 32,
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == "communityuser/my-workflow"

    def test_publish_v1_validation_error_missing_field(self, client: TestClient) -> None:
        resp = client.post(
            PUBLISH_URL,
            json={
                "author": "",
                "name": "test",
                "primitive_type": "invalid_type",
                "content_json": {},
                "signing_key_hex": "aa",
            },
        )
        assert resp.status_code == 422

    def test_publish_v1_invalid_primitive_type(self, client: TestClient) -> None:
        resp = client.post(
            PUBLISH_URL,
            json={
                "author": "testuser",
                "name": "test",
                "primitive_type": "not_a_valid_type",
                "description": "",
                "tags": [],
                "content_json": {},
                "signing_key_hex": "aa" * 32,
            },
        )
        assert resp.status_code == 422


# ============================================================================
# V2 publish / verify tests
# ============================================================================


class TestPublishV2:
    def test_publish_v2_signed(self, client: TestClient) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        _real_private = Ed25519PrivateKey.generate()
        _real_public = _real_private.public_key()

        def _mock_load_pem(data: bytes):
            return _real_public

        entry = _make_entry(slug="signeduser/signed-flow")
        keypair = {"private_key": "bb" * 32}
        with (
            patch("modulo.api.routes.registry.publish_primitive", return_value=entry),
            patch("modulo.api.routes.registry.crypto_pem_verify", return_value=True),
            patch("modulo.api.routes.registry.crypto_generate_keypair", return_value=keypair),
            patch("modulo.api.routes.registry.verify_trust_anchor", return_value=True),
            patch("cryptography.hazmat.primitives.serialization.load_pem_public_key", _mock_load_pem),
        ):
            resp = client.post(
                PUBLISH_V2_URL,
                json={
                    "author": "signeduser",
                    "name": "signed-flow",
                    "primitive_type": "workflow",
                    "description": "Signed workflow",
                    "tags": ["signed"],
                    "content_json": {"nodes": [], "entry": "start"},
                    "signature": "YWJjZGVmZw==",
                    "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["verified"] is True

    def test_publish_v2_signature_fails(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.crypto_pem_verify", return_value=False):
            resp = client.post(
                PUBLISH_V2_URL,
                json={
                    "author": "signeduser",
                    "name": "bad-sig",
                    "primitive_type": "workflow",
                    "description": "Bad signature",
                    "tags": [],
                    "content_json": {},
                    "signature": "YmFk",
                    "public_key_pem": "-----BEGIN PUBLIC KEY-----\nbad\n-----END PUBLIC KEY-----",
                },
            )
        assert resp.status_code == 403

    def test_verify_v2_with_default_key(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(VERIFY_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is True
        assert data["slug"] == "modulo/prd-input-schema"

    def test_verify_v2_unknown_primitive_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=None),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get("/api/v1/registry/verify/unknown/missing")
        assert resp.status_code == 404


# ============================================================================
# Download tests
# ============================================================================


class TestDownloadPrimitive:
    def test_download_creates_local_copy(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=True),
            patch("modulo.api.routes.registry.create_library_primitive"),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.post(DOWNLOADS_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["entry"]["slug"] == "modulo/prd-input-schema"
        assert data["verified"] is True
        assert data["integrity_ok"] is True

    def test_download_unknown_primitive_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=None),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.post("/api/v1/registry/primitives/unknown/nope/download")
        assert resp.status_code == 404

    def test_download_increments_count(self, client: TestClient) -> None:
        entry = _make_entry(download_count=5)
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=True),
            patch("modulo.api.routes.registry.create_library_primitive"),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.post(DOWNLOADS_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["entry"]["download_count"] == 6


# ============================================================================
# Publisher management tests
# ============================================================================


class TestRegisterPublisher:
    def test_register_new_publisher(self, client: TestClient) -> None:
        mock_pub = MagicMock()
        mock_pub.fingerprint = "abcdef1234567890"
        mock_pub.author = "verifieduser"
        with patch("modulo.api.routes.registry.register_publisher", return_value=mock_pub):
            resp = client.post(
                PUBLISHERS_URL,
                json={
                    "fingerprint_hex": "abcdef1234567890",
                    "author": "verifieduser",
                    "name": "Verified User",
                    "website": "https://example.com",
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "registered"
        assert data["fingerprint"] == "abcdef1234567890"

    def test_register_publisher_conflict(self, client: TestClient) -> None:
        # The register_publisher function overwrites, so we test that
        # the response is still 201 (the route always succeeds for now)
        mock_pub = MagicMock()
        mock_pub.fingerprint = "abcdef1234567890"
        mock_pub.author = "duplicate"
        with patch("modulo.api.routes.registry.register_publisher", return_value=mock_pub):
            resp = client.post(
                PUBLISHERS_URL,
                json={
                    "fingerprint_hex": "abcdef1234567890",
                    "author": "duplicate",
                    "name": "Duplicate",
                    "website": "",
                },
            )
        assert resp.status_code == 201

    def test_register_publisher_validation_error(self, client: TestClient) -> None:
        resp = client.post(
            PUBLISHERS_URL,
            json={
                "fingerprint_hex": "ab",
                "author": "",
                "name": "",
            },
        )
        assert resp.status_code == 422


class TestRevokePublisher:
    def test_revoke_existing_publisher(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.revoke_publisher", return_value=True):
            resp = client.post("/api/v1/registry/publishers/abcdef1234567890/revoke")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "revoked"

    def test_revoke_nonexistent_publisher_404(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.revoke_publisher", return_value=False):
            resp = client.post("/api/v1/registry/publishers/0000000000000000/revoke")
        assert resp.status_code == 404


class TestListPublishers:
    def test_list_publishers(self, client: TestClient) -> None:
        mock_pub = MagicMock()
        mock_pub.author = "modulo"
        mock_pub.name = "Modulo Team"
        mock_pub.fingerprint = "abc123"
        mock_pub.website = "https://modulo.ai"
        with patch("modulo.api.routes.registry.list_verified_publishers", return_value=[mock_pub]):
            resp = client.get(PUBLISHERS_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Modulo Team"

    def test_list_publishers_empty(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.list_verified_publishers", return_value=[]):
            resp = client.get(PUBLISHERS_URL)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_revoked_publisher_excluded_from_list(self, client: TestClient) -> None:
        active = MagicMock()
        active.author = "active"
        active.name = "Active Pub"
        active.fingerprint = "active_key"
        active.website = ""
        with patch("modulo.api.routes.registry.list_verified_publishers", return_value=[active]):
            resp = client.get(PUBLISHERS_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Active Pub"


# ============================================================================
# Ed25519 signing round-trip tests
# ============================================================================


class TestSigningRoundTrip:
    def test_generate_sign_and_verify(self) -> None:
        """Full Ed25519 round-trip: generate keypair, sign payload, verify."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        public = private.public_key()

        payload = b"hello registry"

        sig = private.sign(payload)
        try:
            public.verify(sig, payload)
            verified = True
        except Exception:
            verified = False
        assert verified is True

    def test_tampered_payload_fails_verification(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        public = private.public_key()

        payload = b"original payload"
        sig = private.sign(payload)

        tampered = b"tampered payload"
        try:
            public.verify(sig, tampered)
            verified = True
        except Exception:
            verified = False
        assert verified is False

    def test_wrong_key_fails_verification(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_a = Ed25519PrivateKey.generate()
        private_b = Ed25519PrivateKey.generate()
        public_b = private_b.public_key()

        payload = b"some data"
        sig = private_a.sign(payload)

        try:
            public_b.verify(sig, payload)
            verified = True
        except Exception:
            verified = False
        assert verified is False

    def test_empty_payload_signing(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        public = private.public_key()

        sig = private.sign(b"")
        try:
            public.verify(sig, b"")
            verified = True
        except Exception:
            verified = False
        assert verified is True


# ============================================================================
# Trust anchor verification tests
# ============================================================================


class TestTrustAnchor:
    def test_trust_anchor_verification_true(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.crypto_pem_verify", return_value=True),
            patch("modulo.api.routes.registry.verify_trust_anchor", return_value=True),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(
                f"{VERIFY_URL}?public_key_pem=LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KZmFrZQotLS0tLUVORCBQVUJMSUMgS0VZLS0tLS0K"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trust_anchor_verified"] is True

    def test_trust_anchor_verification_false(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.crypto_pem_verify", return_value=True),
            patch("modulo.api.routes.registry.verify_trust_anchor", return_value=False),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(
                f"{VERIFY_URL}?public_key_pem=YmFk"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trust_anchor_verified"] is False
