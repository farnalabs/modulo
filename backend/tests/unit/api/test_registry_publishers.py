"""Unit tests for registry verify endpoint — publisher trust tier lookup."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
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
    """Build a MagicMock RegistryEntry with real values for JSON-serializable fields."""
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


VERIFY_URL = "/api/v1/registry/verify/modulo/prd-input-schema"


class TestVerifyWithTrustTier:
    def test_verify_with_pubkey_returns_trust_tier(self, client: TestClient) -> None:
        mock_pub = MagicMock()
        mock_pub.trust_tier = "green"
        mock_pub.name = "Modulo Team"

        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.crypto_verify_signature", return_value=True),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
            patch("modulo.api.routes.registry.db_get_publisher_by_key", return_value=mock_pub),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(f"{VERIFY_URL}?public_key_hex={'ab' * 32}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trust_tier"] == "green"
        assert data["publisher_name"] == "Modulo Team"
        assert data["verified"] is True

    def test_verify_without_pubkey_returns_no_trust_tier(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.verify_primitive_signature", return_value=True),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="verified"),
        ):
            resp = client.get(VERIFY_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["trust_tier"] is None
        assert data["publisher_name"] is None

    def test_verify_unknown_primitive_returns_404(self, client: TestClient) -> None:
        with patch("modulo.api.routes.registry.get_registry_primitive", return_value=None):
            resp = client.get(f"{VERIFY_URL}?public_key_hex={'ab' * 32}")
        assert resp.status_code == 404

    def test_verify_with_pubkey_no_db_match(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.registry.get_registry_primitive", return_value=_make_entry()),
            patch("modulo.api.routes.registry.crypto_verify_signature", return_value=True),
            patch("modulo.api.routes.registry.get_publisher_status", return_value="community"),
            patch("modulo.api.routes.registry.db_get_publisher_by_key", return_value=None),
            patch("modulo.api.routes.registry.set_rls_org"),
        ):
            resp = client.get(f"{VERIFY_URL}?public_key_hex={'ab' * 32}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trust_tier"] is None
        assert data["publisher_name"] is None
        assert data["publisher_status"] == "community"
