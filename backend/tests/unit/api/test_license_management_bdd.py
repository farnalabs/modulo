"""BDD-derived unit tests for license management endpoint behaviour.

Mirrors the Gherkin scenarios in license_management.feature but runs as plain
pytest unit tests — no pytest-bdd dependency.
"""

import base64
import json
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.license import (
    clear_license,
    get_license,
    parse_and_verify,
    set_public_key,
    store_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32

_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]


def _sign_license_payload(payload: dict, private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    sig_bytes = bytes.fromhex(sig_hex)
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "tier": "team",
        "features": ["sso", "team_rbac", "audit_viewer", "admin_spend_limits"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "acme-org",
    }
    payload.update(overrides)
    return payload


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_license() -> Generator[None, None, None]:
    set_public_key(_TEST_PUB)
    clear_license()
    yield
    clear_license()


@pytest.fixture()
def admin_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
    )
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def non_admin_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
    )
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Upload valid enterprise license ──────────────────────────────────────────


class TestUploadValidLicense:
    URL = "/api/v1/admin/license"

    def test_accepts_valid_enterprise_key(self, admin_client: TestClient) -> None:
        payload = _valid_payload()
        key = _sign_license_payload(payload)
        resp = admin_client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tier"] == "team"
        for feat in ["sso", "team_rbac", "audit_viewer", "admin_spend_limits"]:
            assert feat in data["features"]

    def test_persists_license_in_store(self, admin_client: TestClient) -> None:
        assert get_license() is None
        payload = _valid_payload()
        key = _sign_license_payload(payload)
        admin_client.post(self.URL, json={"license_key": key})
        stored = get_license()
        assert stored is not None
        assert stored.tier == "team"
        assert stored.org_id == "acme-org"


# ── Invalid signature rejected ────────────────────────────────────────────────


class TestInvalidSignature:
    URL = "/api/v1/admin/license"

    def test_tampered_payload_rejected(self, admin_client: TestClient) -> None:
        payload = _valid_payload()
        key = _sign_license_payload(payload)
        parts = key.split(".")
        tampered_b64 = (
            base64.urlsafe_b64encode(json.dumps({"tier": "community"}, separators=(",", ":"), sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        tampered_key = f"{tampered_b64}.{parts[1]}"
        resp = admin_client.post(self.URL, json={"license_key": tampered_key})
        assert resp.status_code == 422
        assert "Signature" in resp.json()["detail"]

    def test_wrong_key_rejected(self, admin_client: TestClient) -> None:
        payload = _valid_payload()
        other_kp = generate_keypair()
        key = _sign_license_payload(payload, private_key=other_kp["private_key"])
        resp = admin_client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 422
        assert "Signature" in resp.json()["detail"]


# ── Expired license rejected ──────────────────────────────────────────────────


class TestExpiredLicense:
    URL = "/api/v1/admin/license"

    def test_expired_license_rejected(self, admin_client: TestClient) -> None:
        payload = _valid_payload(expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
        key = _sign_license_payload(payload)
        resp = admin_client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 422
        assert "expired" in resp.json()["detail"].lower()

    def test_malformed_expiry_rejected(self, admin_client: TestClient) -> None:
        payload = _valid_payload(expires_at="not-a-date")
        key = _sign_license_payload(payload)
        resp = admin_client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 422
        assert "expires_at" in resp.json()["detail"].lower()


# ── Community tier without license ────────────────────────────────────────────


class TestCommunityTier:
    URL = "/api/v1/admin/license"

    def test_community_tier_returned_when_no_license(self, admin_client: TestClient) -> None:
        resp = admin_client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_license"] is False
        assert data["tier"] == "community"
        assert data["features"] == []

    def test_community_tier_has_no_expiry(self, admin_client: TestClient) -> None:
        resp = admin_client.get(self.URL)
        data = resp.json()
        assert data.get("expires_at") is None


# ── License status displayed ──────────────────────────────────────────────────


class TestLicenseStatus:
    URL = "/api/v1/admin/license"

    def test_displays_features_tier_expiry(self, admin_client: TestClient) -> None:
        payload = _valid_payload()
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)

        resp = admin_client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_license"] is True
        assert data["tier"] == "team"
        assert data["org_id"] == "acme-org"
        assert data["expires_at"] is not None

    def test_org_id_displayed(self, admin_client: TestClient) -> None:
        payload = _valid_payload(org_id="specific-org")
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)

        resp = admin_client.get(self.URL)
        data = resp.json()
        assert data["org_id"] == "specific-org"


# ── Features unlocked after license ────────────────────────────────────────────


class TestFeaturesAfterLicense:
    URL = "/api/v1/admin/license"

    def test_enterprise_features_listed(self, admin_client: TestClient) -> None:
        payload = _valid_payload()
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)

        resp = admin_client.get(self.URL)
        data = resp.json()
        assert "sso" in data["features"]
        assert "team_rbac" in data["features"]

    def test_community_tier_no_team_features(self, admin_client: TestClient) -> None:
        resp = admin_client.get(self.URL)
        data = resp.json()
        assert "sso" not in data["features"]


# ── License badge data ────────────────────────────────────────────────────────


class TestLicenseBadgeData:
    URL = "/api/v1/admin/license"

    def test_badge_data_returns_tier_and_expiry(self, admin_client: TestClient) -> None:
        expiry = (datetime.now(UTC) + timedelta(days=365)).isoformat()
        payload = _valid_payload(expires_at=expiry)
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)

        resp = admin_client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_license"] is True
        assert data["tier"] == "team"
        assert data["expires_at"] is not None

    def test_badge_shows_community_when_no_license(self, admin_client: TestClient) -> None:
        resp = admin_client.get(self.URL)
        data = resp.json()
        assert data["has_license"] is False
        assert data["tier"] == "community"
        assert data.get("org_id") is None


# ── Non-admin access control ──────────────────────────────────────────────────


class TestNonAdminAccess:
    URL = "/api/v1/admin/license"

    def test_get_license_forbidden_for_non_admin(self, non_admin_client: TestClient) -> None:
        resp = non_admin_client.get(self.URL)
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    def test_post_license_forbidden_for_non_admin(self, non_admin_client: TestClient) -> None:
        resp = non_admin_client.post(self.URL, json={"license_key": "dGVzdA==.dGVzdA=="})
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()
