"""Unit tests for GET /api/v1/license (viewmodel.py license_info endpoint).

FAR-1720: the endpoint mirrors the deployment-level rungs of the canonical
licence ladder (``core.feature_flags._resolve_paid_plan``): the in-memory
licence wins, the env-var key is only trusted after ``parse_and_verify``,
and with no valid licence the response is community / no features /
``is_valid=False``.
"""

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.dependencies import get_settings as dep_get_settings
from modulo.api.main import app
from modulo.api.routes.viewmodel import _resolve_tier
from modulo.core.license import LicenseData, clear_license, set_public_key, store_license
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32

# Known test keypair: signed payloads verify only when set_public_key(_TEST_PUB)
# is in effect (the autouse fixture below manages that).
_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]


def _make_settings(modulo_license_key: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=modulo_license_key,
    )


def _sign_license_payload(payload: dict[str, Any], private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    sig_bytes = bytes.fromhex(sig_hex)
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "team",
        "features": ["sso", "team_rbac", "audit_viewer"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "test-org",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _reset_license_state() -> Generator[None, None, None]:
    """In-memory licence + configured public key are module globals; reset per test."""
    clear_license()
    set_public_key(_TEST_PUB)
    yield
    clear_license()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _get_with_settings(client: TestClient, settings: Settings) -> Any:
    old_override = app.dependency_overrides.get(dep_get_settings)
    app.dependency_overrides[dep_get_settings] = lambda: settings
    try:
        return client.get("/api/v1/license")
    finally:
        if old_override is not None:
            app.dependency_overrides[dep_get_settings] = old_override
        else:
            app.dependency_overrides.pop(dep_get_settings, None)


class TestLicenseInfo:
    URL = "/api/v1/license"

    def test_no_license_returns_community_invalid(self, client: TestClient) -> None:
        """No in-memory licence and no env-var key -> community, no features, is_valid=False.

        FAR-1720: the old code reported ``is_valid=True`` for the unlicensed
        state; the unified contract reports ``False``.
        """
        resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "community"
        assert not body["features"]
        assert body["is_valid"] is False

    def test_invalid_env_key_is_not_trusted(self, client: TestClient) -> None:
        """An env-var key present but failing parse_and_verify -> community, is_valid=False.

        FAR-1720 prove-the-fix: the old code trusted mere presence and reported
        ``tier="team"`` / ``is_valid=True`` for any non-empty env-var key.
        """
        resp = _get_with_settings(client, _make_settings(modulo_license_key="not-a-valid-key"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "community"
        assert not body["features"]
        assert body["is_valid"] is False

    def test_valid_env_key_reports_verified_tier_and_features(self, client: TestClient) -> None:
        """A signed env-var key verifies -> its tier/features, is_valid=True."""
        key = _sign_license_payload(_valid_payload(features=["sso", "audit_viewer"]))
        resp = _get_with_settings(client, _make_settings(modulo_license_key=key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "team"
        assert body["features"] == ["sso", "audit_viewer"]
        assert body["is_valid"] is True

    def test_stored_signed_license_wins_over_env_var_state(self, client: TestClient) -> None:
        """In-memory licence (rung 1) beats the env-var rung.

        FAR-1720 prove-the-fix: with the env var absent (old code: community)
        a stored signed licence's tier/features are reported, is_valid=True.
        With an invalid env key present (old code: team) the stored licence
        still wins.
        """
        stored = LicenseData(
            tier="team",
            features=["sso", "team_rbac", "audit_viewer", "admin_spend_limits"],
            expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            org_id="stored-org",
            raw_payload={},
            raw_key="stored-key",
        )
        store_license("stored-key", stored)

        # Env var absent: old code reported community; new reports the licence.
        resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "team"
        assert body["features"] == ["sso", "team_rbac", "audit_viewer", "admin_spend_limits"]
        assert body["is_valid"] is True

        # Invalid env key present: old code reported team/[notifications]/True
        # from presence alone; the stored licence still wins with real features.
        resp = _get_with_settings(client, _make_settings(modulo_license_key="not-a-valid-key"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "team"
        assert body["features"] == ["sso", "team_rbac", "audit_viewer", "admin_spend_limits"]
        assert body["is_valid"] is True

    def test_response_shape_has_no_extra_fields(self, client: TestClient) -> None:
        """The public contract stays exactly tier / features / is_valid."""
        resp = client.get(self.URL)
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"tier", "features", "is_valid"}

    def test_publicly_accessible(self, client: TestClient) -> None:
        """The /api/v1/license endpoint requires no auth."""
        resp = client.get(self.URL)
        assert resp.status_code == 200


class TestResolveTier:
    """``viewmodel._resolve_tier`` mirrors the canonical licence ladder.

    FAR-1720 review follow-up: the in-file sibling still trusted
    ``settings.modulo_license_key`` on mere presence, so it could disagree
    with the now-verified ``_resolve_license_info`` for the same deployment
    state. It now verifies the key via ``parse_and_verify`` like the sibling
    and the canonical ladder (org key -> in-memory licence -> env key).
    """

    def test_invalid_env_key_is_not_trusted(self) -> None:
        """A present-but-unverifiable env key -> community (prove-the-fix).

        The pre-change resolver returned ``"team"`` for any non-empty key.
        """
        assert _resolve_tier(_make_settings(modulo_license_key="not-a-valid-key")) == "community"

    def test_valid_env_key_reports_verified_tier(self) -> None:
        """A signed env key verifies -> its own tier."""
        key = _sign_license_payload(_valid_payload(tier="team"))
        assert _resolve_tier(_make_settings(modulo_license_key=key)) == "team"

    def test_no_license_is_community(self) -> None:
        """No in-memory licence and no env key -> community."""
        assert _resolve_tier(_make_settings()) == "community"

    def test_stored_license_wins_over_env_key(self) -> None:
        """The in-memory licence (rung 2) beats the env-var rung (rung 3)."""
        stored = LicenseData(
            tier="team",
            features=["sso"],
            expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            org_id="stored-org",
            raw_payload={},
            raw_key="stored-key",
        )
        store_license("stored-key", stored)
        assert _resolve_tier(_make_settings(modulo_license_key="not-a-valid-key")) == "team"

    def test_org_key_takes_precedence(self) -> None:
        """A valid org-level key (rung 1) is resolved before the env-var rung."""
        org = MagicMock()
        org.settings_json = {"license_key": _sign_license_payload(_valid_payload(tier="team"))}
        assert _resolve_tier(_make_settings(modulo_license_key="not-a-valid-key"), org=org) == "team"

    def test_invalid_org_key_falls_through(self) -> None:
        """An unverifiable org key is not trusted; resolution falls through."""
        org = MagicMock()
        org.settings_json = {"license_key": "garbage"}
        assert _resolve_tier(_make_settings(), org=org) == "community"

    def test_org_without_key_falls_through(self) -> None:
        """An org present but carrying no license_key resolves via the deployment rungs."""
        org = MagicMock()
        org.settings_json = {}
        assert _resolve_tier(_make_settings(modulo_license_key="not-a-valid-key"), org=org) == "community"
