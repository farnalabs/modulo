"""Integration-level tests for registry protocol v2 endpoints — verify, publish, download.

Covers the public_key_pem verify path, duplicate slug publish (last-write-wins),
signature failure handling, bundle integrity mismatch, and SQLAlchemy error handling.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


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
    entry.signing_key_fingerprint = "abc123"
    entry.author = "modulo"
    entry.name = "prd-input-schema"
    entry.version = "1.0"
    entry.primitive_type = "schema"
    entry.description = "test"
    entry.tags = []
    entry.content_json = {}
    entry.ed25519_signature_hex = "ab" * 32
    entry.checksum_sha256 = "deadbeef"
    entry.published_at = datetime(2025, 1, 1, tzinfo=UTC)
    entry.download_count = 0
    for k, v in overrides.items():
        setattr(entry, k, v)
    return entry


@pytest.fixture()
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


VERIFY_URL = "/api/v1/registry/verify/modulo/prd-input-schema"
PUBLISH_URL = "/api/v1/registry/publish"
DOWNLOAD_URL = "/api/v1/registry/primitives/modulo/prd-input-schema/download"


class TestVerifyWithPem:
    def test_verify_with_pem_returns_trust_anchor(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.crypto_pem_verify", return_value=True) as mock_pem_verify,
            patch("modulo.api.routes.registry.verify_trust_anchor", return_value=True) as mock_ta,
        ):
            resp = client.get(f"{VERIFY_URL}?public_key_pem={'ab' * 32}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is True
        assert data["trust_anchor_verified"] is True
        mock_pem_verify.assert_called_once()
        mock_ta.assert_called_once()

    def test_verify_with_pem_and_failed_verify(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="community"),
            patch("modulo.api.routes.registry.crypto_pem_verify", return_value=False) as mock_pem_verify,
            patch("modulo.api.routes.registry.verify_trust_anchor", return_value=False) as mock_ta,
        ):
            resp = client.get(f"{VERIFY_URL}?public_key_pem={'ab' * 32}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is False
        assert data["trust_anchor_verified"] is False
        mock_pem_verify.assert_called_once()
        mock_ta.assert_called_once()


class TestV2Publish:
    PUBLISH_BODY = {
        "author": "testauthor",
        "name": "test-primitive",
        "primitive_type": "schema",
        "description": "test",
        "tags": [],
        "content_json": {},
        "signature": "dGVzdHNpZw==",
        "public_key_pem": (
            "-----BEGIN PUBLIC KEY-----\n"
            "MCowBQYDK2VwAyEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
            "-----END PUBLIC KEY-----"
        ),
    }

    def test_publish_duplicate_slug_overwrites(self, client: TestClient) -> None:
        entry_1 = _make_entry(slug="testauthor/test-primitive")
        entry_2 = _make_entry(slug="testauthor/test-primitive", description="overwritten")

        with (
            patch("modulo.api.routes.registry.crypto_pem_verify", return_value=True),
            patch("modulo.api.routes.registry.verify_trust_anchor", return_value=True),
            patch(
                "modulo.core.registry.crypto.generate_keypair",
                return_value={"private_key": "aa" * 32, "public_key": "bb" * 32, "fingerprint": "cc" * 16},
            ),
            patch("modulo.api.routes.registry.publish_primitive", side_effect=[entry_1, entry_2]) as mock_publish,
        ):
            resp1 = client.post(PUBLISH_URL, json=self.PUBLISH_BODY)
            assert resp1.status_code == 201
            assert resp1.json()["slug"] == "testauthor/test-primitive"

            resp2 = client.post(PUBLISH_URL, json=self.PUBLISH_BODY)
            assert resp2.status_code == 201
            assert resp2.json()["slug"] == "testauthor/test-primitive"

        assert mock_publish.call_count == 2

    def test_publish_signature_failure_returns_403(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.crypto_pem_verify", return_value=False):
            resp = client.post(PUBLISH_URL, json=self.PUBLISH_BODY)

        assert resp.status_code == 403
        assert "signature" in resp.json()["detail"].lower()


class TestDownload:
    def test_bundle_integrity_mismatch_returns_integrity_ok_false(self, client: TestClient) -> None:
        entry = _make_entry(publisher_status="community")
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=False),
            patch("modulo.api.routes.registry.set_rls_org"),
            patch("modulo.api.routes.registry.create_library_primitive"),
        ):
            resp = client.post(DOWNLOAD_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["integrity_ok"] is False
        assert data["verified"] is True


class TestDownloadErrors:
    def test_download_programming_error_returns_501(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=True),
            patch("modulo.api.routes.registry.set_rls_org"),
            patch(
                "modulo.api.routes.registry.create_library_primitive",
                side_effect=ProgrammingError("stmt", {}, "table not found"),
            ),
        ):
            resp = client.post(DOWNLOAD_URL)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_download_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        entry = _make_entry()
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=entry),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.verify_bundle_integrity", return_value=True),
            patch("modulo.api.routes.registry.set_rls_org"),
            patch(
                "modulo.api.routes.registry.create_library_primitive",
                side_effect=SQLAlchemyError("connection lost"),
            ),
        ):
            resp = client.post(DOWNLOAD_URL)

        assert resp.status_code == 503


class TestVerifyErrors:
    def test_verify_hex_programming_error_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.crypto_verify_signature", return_value=True),
            patch(
                "modulo.api.routes.registry.db_get_publisher_by_key",
                side_effect=ProgrammingError("stmt", {}, "table not found"),
            ),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(f"{VERIFY_URL}?public_key_hex={'ab' * 32}")

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_verify_hex_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.crypto_verify_signature", return_value=True),
            patch(
                "modulo.api.routes.registry.db_get_publisher_by_key",
                side_effect=SQLAlchemyError("connection lost"),
            ),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(f"{VERIFY_URL}?public_key_hex={'ab' * 32}")

        assert resp.status_code == 503
