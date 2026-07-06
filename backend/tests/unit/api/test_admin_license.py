"""Unit tests for /api/v1/admin/license endpoints."""

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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

# Use a known test keypair so we can sign payloads for testing
_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


def _sign_license_payload(payload: dict, private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    sig_bytes = bytes.fromhex(sig_hex)
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _make_valid_payload(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "tier": "team",
        "features": ["sso", "team_rbac", "audit_viewer"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "test-org",
    }
    payload.update(overrides)
    return payload


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_license_state() -> Generator[None, None, None]:
    clear_license()
    set_public_key(_TEST_PUB)
    yield
    clear_license()


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = _make_mock_session
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
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = _make_mock_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Core license parsing tests ───────────────────────────────────────────


class TestParseAndVerify:
    def test_parses_valid_license(self) -> None:
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "team"
        assert "sso" in result.license_data.features
        assert result.license_data.org_id == "test-org"

    def test_rejects_tampered_payload(self) -> None:
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        parts = key.split(".")
        tampered_payload_b64 = base64.urlsafe_b64encode(
            json.dumps({"tier": "community"}, separators=(",", ":"), sort_keys=True).encode()
        ).decode().rstrip("=")
        tampered_key = f"{tampered_payload_b64}.{parts[1]}"
        result = parse_and_verify(tampered_key)
        assert result.valid is False
        assert "Signature" in (result.error or "")

    def test_rejects_expired_license(self) -> None:
        payload = _make_valid_payload(
            expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
        )
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.valid is False
        assert "expired" in (result.error or "").lower()

    def test_rejects_malformed_base64(self) -> None:
        result = parse_and_verify("not-valid-base64!!.also-not-valid")
        assert result.valid is False
        assert result.error is not None

    def test_rejects_missing_dot(self) -> None:
        result = parse_and_verify("no-dot-separator")
        assert result.valid is False
        assert "expected" in (result.error or "").lower()

    def test_rejects_wrong_public_key(self) -> None:
        payload = _make_valid_payload()
        other_kp = generate_keypair()
        key = _sign_license_payload(payload, private_key=other_kp["private_key"])
        result = parse_and_verify(key)
        assert result.valid is False
        assert "Signature" in (result.error or "")

    def test_accepts_community_tier_no_expiry(self) -> None:
        payload = {
            "tier": "community",
            "features": [],
            "org_id": "test-org",
        }
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "community"
        assert result.license_data.expires_at == ""


class TestStoreAndGetLicense:
    def test_store_license(self) -> None:
        assert get_license() is None
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)
        stored = get_license()
        assert stored is not None
        assert stored.tier == "team"
        assert stored.org_id == "test-org"

    def test_clear_license(self) -> None:
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)
        assert get_license() is not None
        clear_license()
        assert get_license() is None


# ── API endpoint tests ───────────────────────────────────────────────────


class TestGetLicense:
    URL = "/api/v1/admin/license"

    def _mock_org(self, settings_json: dict | None = None) -> MagicMock:
        org = MagicMock()
        org.settings_json = settings_json
        return org

    def test_returns_no_license_when_none_set(self, client: TestClient) -> None:
        org = self._mock_org(settings_json=None)
        with patch("modulo.api.routes.admin_license.get_organisation", new=AsyncMock(return_value=org)):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_license"] is False
        assert data["tier"] == "community"
        assert data["features"] == []

    def test_returns_license_when_set(self, client: TestClient) -> None:
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        result = parse_and_verify(key)
        assert result.license_data is not None
        store_license(key, result.license_data)

        org = self._mock_org(settings_json={})
        with patch("modulo.api.routes.admin_license.get_organisation", new=AsyncMock(return_value=org)):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_license"] is True
        assert data["tier"] == "team"
        assert "sso" in data["features"]
        assert data["org_id"] == "test-org"

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_requires_admin(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403


class TestUploadLicense:
    URL = "/api/v1/admin/license"

    def test_accepts_valid_license(self, client: TestClient) -> None:
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        resp = client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tier"] == "team"
        assert "sso" in data["features"]
        assert data["org_id"] == "test-org"

    def test_persists_license(self, client: TestClient) -> None:
        payload = _make_valid_payload()
        key = _sign_license_payload(payload)
        resp = client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 200

        resp2 = client.get(self.URL)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["has_license"] is True
        assert data2["tier"] == "team"

    def test_rejects_invalid_signature(self, client: TestClient) -> None:
        payload = _make_valid_payload()
        wrong_kp = generate_keypair()
        key = _sign_license_payload(payload, private_key=wrong_kp["private_key"])
        resp = client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 422
        assert "Signature" in resp.json()["detail"]

    def test_rejects_expired_license(self, client: TestClient) -> None:
        payload = _make_valid_payload(
            expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
        )
        key = _sign_license_payload(payload)
        resp = client.post(self.URL, json={"license_key": key})
        assert resp.status_code == 422
        assert "expired" in resp.json()["detail"].lower()

    def test_rejects_malformed_key(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"license_key": "not-a-valid-key"})
        assert resp.status_code == 422

    def test_rejects_empty_key(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"license_key": ""})
        assert resp.status_code == 422

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"license_key": "dGVzdA==.dGVzdA=="})
        assert resp.status_code in (401, 403)

    def test_requires_admin(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL, json={"license_key": "dGVzdA==.dGVzdA=="})
        assert resp.status_code == 403
