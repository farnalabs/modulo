"""Unit tests for the admin feature-flags API endpoint."""

import asyncio
import base64
import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import admin_feature_flags
from modulo.api.routes.admin_feature_flags import (
    _build_registry,
    _emit_org_flag_override_audit,
    _enforce_team_tier_gate,
    _license_is_valid,
    _resolve_tier,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    LICENCE_GRACE_DAYS,
    LicenseData,
    LicenseValidation,
    clear_license,
    set_public_key,
    store_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble

_VALID_32 = "a" * 32

# FAR-1733: known test keypair — a signed payload verifies only while
# set_public_key(_TEST_PUB) is in effect (the autouse fixture below manages
# that per test), so the env-key verification paths run for real.
_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_internal_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_dev_mode=True,
    )


@pytest.fixture
def internal_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_internal_settings
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="operator",
        is_system_admin=True,
        key_grants=frozenset({"system_maps_access"}),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _settings_with_key(modulo_license_key: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=modulo_license_key,
    )


def _sign_license_payload(payload: dict[str, Any], private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig_hex)).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _valid_license_key(tier: str = "team") -> str:
    payload: dict[str, Any] = {
        "tier": tier,
        "features": ["sso", "team_rbac", "audit_viewer"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "test-org",
    }
    return _sign_license_payload(payload)


@pytest.fixture(autouse=True)
def _reset_license_state() -> Generator[None, None, None]:
    """FAR-1733: in-memory licence + configured public key are module globals.

    Reset both around every test so a stored licence or a test keypair never
    leaks into a sibling test's licence resolution.
    """
    clear_license()
    set_public_key(_TEST_PUB)
    yield
    clear_license()
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_internal_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=True,
        key_grants=frozenset({"system_maps_access"}),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def ordinary_internal_flag_client() -> Generator[TestClient, None, None]:
    """System-config caller without the internal System Maps contract."""
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=True,
        key_grants=frozenset({"library.search.in_dev"}),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def no_org_client() -> Generator[TestClient, None, None]:
    """System admin whose principal has no organisation_id.

    Exercises the org-scoped feature-flag override guard that rejects such
    principals with 403 before any org lookup.
    """
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=None,
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=True,
        key_grants=frozenset({"library.search.in_dev"}),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def non_sys_admin_client() -> Generator[TestClient, None, None]:
    """Org admin (not system admin) — must get 403 on feature-flag endpoints."""
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=False,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_registry_overrides() -> Generator[None, None, None]:
    """Clear the class-level FeatureFlagRegistry._overrides after each test.

    The toggle endpoint no longer mutates this shared class variable (it
    persists org overrides instead), but the hygiene stays: any test that
    touches the system-level override mechanism must not leak into other test
    modules that construct a registry (e.g. tests/unit/core/test_plan_context.py)
    in the same pytest process.
    """
    yield
    FeatureFlagRegistry._overrides.clear()


@pytest.fixture(autouse=True)
def _patch_resolve_tier() -> Generator[None, None, None]:
    """Patch _resolve_tier to return 'community' by default.

    The toggle and org-override endpoints now call _resolve_tier to check the
    tier gate. Without this patch, the endpoint tries to hit the DB via
    resolve_plan_context and fails with a MagicMock await error. Tests that
    need a specific tier can override this mock.
    """
    with patch(
        "modulo.api.routes.admin_feature_flags._resolve_tier",
        new_callable=AsyncMock,
        return_value="community",
    ):
        yield


@pytest.fixture(autouse=True)
def _patch_org_settings_writer() -> Generator[None, None, None]:
    """Apply the FAR-1830 atomic settings patch in-memory for the unit tier.

    ``update_org_settings_patch`` writes through the real DB (a JSONB statement
    the mocked session cannot run) and its fail-closed tenant guard needs a
    bound RLS context. The unit tier has neither, so this mirrors the helper's
    top-level merge semantics onto whichever org the route's (patched)
    ``get_organisation`` returns. The real helper is covered by
    ``tests/integration/crud/test_org_settings_patch.py``.
    """

    async def _apply(session, organisation_id, patch=None, delete_keys=None, **_kwargs):
        org = await admin_feature_flags.get_organisation(session, organisation_id)
        settings = dict(org.settings_json or {}) if org is not None and isinstance(org.settings_json, dict) else {}
        settings.update(patch or {})
        for key in delete_keys or []:
            settings.pop(key, None)
        if org is not None:
            org.settings_json = settings
        return settings

    with patch("modulo.api.routes.admin_feature_flags.update_org_settings_patch", new=_apply):
        yield


def _mock_registry() -> FeatureFlagRegistry:
    """Return a FeatureFlagRegistry with hardcoded flags (no DB)."""
    return FeatureFlagRegistry(current_tier="community", has_license_key=False)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags
# ---------------------------------------------------------------------------


class TestListFeatureFlags:
    def test_ordinary_caller_cannot_see_system_maps(self, ordinary_internal_flag_client: TestClient) -> None:
        """library.search.in_dev alone does not grant system_maps access."""
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            response = ordinary_internal_flag_client.get("/api/v1/admin/feature-flags")
        assert response.status_code == 200
        names = {flag["name"] for flag in response.json()["flags"]}
        assert "system_maps" not in names

    def test_internal_caller_can_see_system_maps(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            response = client.get("/api/v1/admin/feature-flags")
        assert response.status_code == 200
        names = {flag["name"] for flag in response.json()["flags"]}
        assert "system_maps" in names

    def test_returns_200(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 200

    def test_returns_license_block(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "license" in body
        assert body["license"]["tier"] in ("community", "team")
        assert "has_license_key" in body["license"]
        # FAR-1733: is_valid is no longer hardcoded True — with no licence
        # resolvable (no in-memory licence, no env key) it reports False.
        # This assertion FAILS against the old code (constant True).
        assert body["license"]["is_valid"] is False

    def test_returns_license_block_valid_with_stored_license(self, client: TestClient) -> None:
        """A stored in-memory licence makes the licence block report valid."""
        stored = LicenseData(
            tier="team",
            features=["sso"],
            expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            org_id="stored-org",
            raw_payload={},
            raw_key="stored-key",
        )
        store_license("stored-key", stored)
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert body["license"]["is_valid"] is True

    def test_returns_flags_list(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "flags" in body
        assert body["flags"]
        for flag in body["flags"]:
            assert "name" in flag
            assert "description" in flag
            assert "tier" in flag
            assert "currently_active" in flag

    def test_returns_would_activate(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "would_activate" in body

    def test_community_tier_has_team_flags_in_would_activate(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        if body["license"]["tier"] == "community":
            assert body["would_activate"]
            for flag in body["would_activate"]:
                assert flag["tier"] != "community"

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/feature-flags")
        assert resp.status_code in (401, 403)

    def test_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_org_override_reflected_in_flags_payload(self, client: TestClient) -> None:
        """An org-level feature_overrides entry must win over the registry default
        in the /feature-flags payload the whole app (plan store) reads.

        Regression: without the override overlay, the admin UI's "Override →
        Enabled" only affected the admin view's own session, so an org-level
        enable never took effect app-wide.
        """
        org = MagicMock()
        org.settings_json = {"feature_overrides": {"sso": True}}
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new=AsyncMock(return_value=org),
            ),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        # sso is a team-tier flag, inactive on the community mock registry — the
        # org override flips it to True in the payload.
        sso = next(f for f in body["flags"] if f["name"] == "sso")
        assert sso["currently_active"] is True

    def test_org_override_excluded_from_would_activate(self, client: TestClient) -> None:
        """An org override (either direction) supersedes the tier-gap suggestion.

        Regression: a community org that enables a team-tier flag via org
        override saw it BOTH in ``flags`` as ``currently_active: true`` AND in
        ``would_activate`` (because ``tier_gap_flags()`` only inspects the
        registry's computed state, unaware of the override). An org that has
        made an explicit choice must not be told it "would activate on tier
        upgrade".
        """
        org = MagicMock()
        org.settings_json = {"feature_overrides": {"sso": True}}
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new=AsyncMock(return_value=org),
            ),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        sso = next(f for f in body["flags"] if f["name"] == "sso")
        assert sso["currently_active"] is True
        assert all(f["name"] != "sso" for f in body["would_activate"])

    def test_org_override_read_failure_falls_back_to_defaults(self, client: TestClient) -> None:
        """A failed org-override read (e.g. mock/DB session without org data) must
        fall back to the registry defaults instead of failing the request."""
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 200
        body = resp.json()
        sso = next(f for f in body["flags"] if f["name"] == "sso")
        assert sso["currently_active"] is False

    def test_missing_org_row_returns_registry_defaults(self, client: TestClient) -> None:
        """``_read_org_overrides`` with a vanished org row returns ``{}`` — the
        shared bool-only reader's org-None branch answers, no exception."""
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = client.get("/api/v1/admin/feature-flags")

        assert resp.status_code == 200
        body = resp.json()
        sso = next(f for f in body["flags"] if f["name"] == "sso")
        assert sso["currently_active"] is False


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags/{flag_name}
# ---------------------------------------------------------------------------


class TestGetFeatureFlag:
    def test_ordinary_caller_cannot_inspect_system_maps(self, ordinary_internal_flag_client: TestClient) -> None:
        """library.search.in_dev alone does not grant system_maps access."""
        response = ordinary_internal_flag_client.get("/api/v1/admin/feature-flags/system_maps")
        assert response.status_code == 403

    def test_internal_caller_can_inspect_system_maps(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            response = client.get("/api/v1/admin/feature-flags/system_maps")
        assert response.status_code == 200
        assert response.json()["name"] == "system_maps"

    def test_returns_200_for_known_flag(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "sso"
        assert body["tier"] == "team"

    def test_returns_404_for_unknown_flag(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags/nonexistent_flag")
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body

    def test_returns_404_for_retired_internal_flag(self, client: TestClient) -> None:
        """Retired internal flags are hidden even from the system-admin GET."""
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags/lifecycle_map_journeys")
        assert resp.status_code == 404
        assert "detail" in resp.json()

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code in (401, 403)

    def test_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_org_override_reflected_in_single_flag_payload(self, client: TestClient) -> None:
        """The single-flag GET must apply the org override so it agrees with the
        list endpoint (which the frontend consumes). Regression: with an org
        override persisted, the list showed the flag active while the
        single-flag GET still showed the registry default — the list and the
        debug endpoint disagreed (observed live 2026-09-09)."""
        org = _org_with_overrides(sso=True)
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 200
        # sso is a team-tier flag, inactive on the community mock registry —
        # the org override flips it to True in the single-flag payload too.
        assert resp.json()["currently_active"] is True

    def test_org_override_false_wins_in_single_flag_payload(self, client: TestClient) -> None:
        """A persisted org override of False must hide a flag that is active by
        tier — the override wins in BOTH directions, matching the list."""
        org = _org_with_overrides(eval_system=False)
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
        ):
            resp = client.get("/api/v1/admin/feature-flags/eval_system")
        assert resp.status_code == 200
        # eval_system is community-tier (active on the mock registry);
        # the org override flips it to False.
        assert resp.json()["currently_active"] is False

    def test_single_flag_get_agrees_with_list_payload(self, client: TestClient) -> None:
        """Both GET endpoints must report the same effective currently_active
        for the caller's org when an org override exists."""
        org = _org_with_overrides(sso=True)
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
        ):
            single = client.get("/api/v1/admin/feature-flags/sso")
            listing = client.get("/api/v1/admin/feature-flags")
        assert single.status_code == 200
        assert listing.status_code == 200
        sso = next(f for f in listing.json()["flags"] if f["name"] == "sso")
        assert single.json()["currently_active"] == sso["currently_active"]

    def test_org_settings_read_failure_falls_back_to_default(self, client: TestClient) -> None:
        """A missing/unreadable org settings payload must fall back to the
        registry default instead of failing the request."""
        org = MagicMock()
        org.settings_json = None
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 200
        assert resp.json()["currently_active"] is False


# ---------------------------------------------------------------------------
# ProgrammingError -> 501
# ---------------------------------------------------------------------------


class TestProgrammingError:
    def test_list_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "NOT_IMPLEMENTED"

    def test_get_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "NOT_IMPLEMENTED"

    def test_toggle_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "NOT_IMPLEMENTED"


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/feature-flags/{flag_name} — toggle
# ---------------------------------------------------------------------------


class TestToggleFeatureFlag:
    def test_ordinary_caller_cannot_enable_system_maps(self, ordinary_internal_flag_client: TestClient) -> None:
        """library.search.in_dev alone does not grant system_maps access."""
        response = ordinary_internal_flag_client.put("/api/v1/admin/feature-flags/system_maps", json={"enabled": True})
        assert response.status_code == 403

    def test_internal_caller_can_enable_system_maps(self, client: TestClient) -> None:
        org = _org_with_overrides()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            response = client.put("/api/v1/admin/feature-flags/system_maps", json={"enabled": True})
        assert response.status_code == 200
        assert response.json()["name"] == "system_maps"

    def test_toggle_known_flag_returns_200(self, client: TestClient) -> None:
        org = _org_with_overrides()
        redis_mock = AsyncMock()
        redis_mock.get.return_value = None
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._resolve_tier",
                new_callable=AsyncMock,
                return_value="community",
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "webhook_trigger"
        assert "overridden" in body

    def test_toggle_unknown_flag_returns_404(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.put(
                "/api/v1/admin/feature-flags/nonexistent",
                json={"enabled": True},
            )
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body

    def test_toggle_retired_internal_flag_returns_404_without_writing(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._write_org_override",
                new_callable=AsyncMock,
            ) as write_override,
        ):
            resp = client.put(
                "/api/v1/admin/feature-flags/lifecycle_map_journeys",
                json={"enabled": True},
            )
        assert resp.status_code == 404
        write_override.assert_not_awaited()

    def test_toggle_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(
            "/api/v1/admin/feature-flags/sso",
            json={"enabled": True},
        )
        assert resp.status_code in (401, 403)

    def test_toggle_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.put(
                "/api/v1/admin/feature-flags/sso",
                json={"enabled": True},
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/feature-flags/{flag_name} — durable persistence
# ---------------------------------------------------------------------------
# Regression: the toggle endpoint used to call ``registry.set_override`` on a
# per-request registry — an in-memory mutation with no DB write. The response
# claimed ``overridden: true`` while the very next request (or process) saw the
# flag inactive again (observed live on app.modulo.run 2026-09-09). The toggle
# now persists via the same org-settings path as the org-override endpoint.


class TestTogglePersistence:
    def test_toggle_persists_and_second_request_sees_flag_active(self, client: TestClient) -> None:
        """A toggle must survive a fresh request: the org override lands in the
        org's settings (the durable store) and the subsequent list read — which
        overlays org overrides — reports the flag as active."""
        org = _org_with_overrides()
        redis_mock = AsyncMock()
        redis_mock.get.return_value = None
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
            assert resp.status_code == 200
            body = resp.json()
            assert body["overridden"] is True
            assert body["currently_active"] is True
            assert org.settings_json["feature_overrides"]["webhook_trigger"] is True
            redis_mock.delete.assert_awaited_once_with(
                "feature-flags:00000000-0000-0000-0000-000000000001:public",
                "feature-flags:00000000-0000-0000-0000-000000000001:internal",
            )
            listing = client.get("/api/v1/admin/feature-flags")
            assert listing.status_code == 200
            flag = next(f for f in listing.json()["flags"] if f["name"] == "webhook_trigger")
            assert flag["currently_active"] is True

    def test_toggle_off_persists_and_second_request_sees_flag_inactive(self, client: TestClient) -> None:
        """Disabling via toggle must persist too: eval_system is active on
        community tier by default, so only the persisted org override can flip
        the second request's read to inactive."""
        org = _org_with_overrides()
        redis_mock = AsyncMock()
        redis_mock.get.return_value = None
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/eval_system", json={"enabled": False})
            assert resp.status_code == 200
            assert org.settings_json["feature_overrides"]["eval_system"] is False
            listing = client.get("/api/v1/admin/feature-flags")
            assert listing.status_code == 200
            flag = next(f for f in listing.json()["flags"] if f["name"] == "eval_system")
            assert flag["currently_active"] is False

    def test_toggle_write_failure_never_claims_overridden(self, client: TestClient) -> None:
        """A failed durable write must return an error, never ``overridden: true``
        for a write that did not happen."""
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 500
        body = resp.json()
        assert "overridden" not in body
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_toggle_survives_cache_invalidation_failure(self, client: TestClient) -> None:
        """The best-effort cache delete must never fail an already-committed
        toggle (the DB write is the source of truth)."""
        org = _org_with_overrides()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["overridden"] is True


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/feature-flags/{flag_name}/org-override — Redis cache
# invalidation on set/clear
# ---------------------------------------------------------------------------
# The /feature-flags list endpoint caches its payload (60s TTL) per org. An
# admin toggling an org override must invalidate that cache so the change takes
# effect app-wide immediately — otherwise the plan store reads a stale payload
# for up to 60s. The invalidation is best-effort: a Redis failure must not fail
# the already-committed DB mutation.


def _org_with_overrides(**overrides: bool) -> MagicMock:
    org = MagicMock()
    org.settings_json = {"feature_overrides": overrides} if overrides else {}
    return org


class TestOrgOverrideCacheInvalidation:
    _ORG_ID = "00000000-0000-0000-0000-000000000001"

    def test_set_retired_internal_override_returns_404_without_writing(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._write_org_override",
                new_callable=AsyncMock,
            ) as write_override,
        ):
            resp = client.put(
                "/api/v1/admin/feature-flags/lifecycle_map_journeys/org-override",
                json={"enabled": True},
            )
        assert resp.status_code == 404
        write_override.assert_not_awaited()

    def test_set_system_maps_override_is_denied_for_ordinary_caller(
        self, ordinary_internal_flag_client: TestClient
    ) -> None:
        """library.search.in_dev alone does not grant system_maps access."""
        response = ordinary_internal_flag_client.put(
            "/api/v1/admin/feature-flags/system_maps/org-override", json={"enabled": True}
        )
        assert response.status_code == 403

    def test_set_system_maps_override_succeeds_for_internal_caller(self, internal_client: TestClient) -> None:
        org = _org_with_overrides()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            response = internal_client.put(
                "/api/v1/admin/feature-flags/system_maps/org-override", json={"enabled": True}
            )
        assert response.status_code == 200
        assert response.json()["override"] is True

    def test_set_org_override_invalidates_redis_cache(self, client: TestClient) -> None:
        org = _org_with_overrides()
        redis_mock = AsyncMock()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger/org-override", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["override"] is True
        redis_mock.delete.assert_awaited_once_with(
            f"feature-flags:{self._ORG_ID}:public",
            f"feature-flags:{self._ORG_ID}:internal",
        )

    def test_clear_org_override_invalidates_redis_cache(self, client: TestClient) -> None:
        org = _org_with_overrides(webhook_trigger=True)
        redis_mock = AsyncMock()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.delete("/api/v1/admin/feature-flags/webhook_trigger/org-override")
        assert resp.status_code == 200
        assert resp.json()["override"] is None
        redis_mock.delete.assert_awaited_once_with(
            f"feature-flags:{self._ORG_ID}:public",
            f"feature-flags:{self._ORG_ID}:internal",
        )

    def test_set_org_override_survives_cache_invalidation_failure(self, client: TestClient) -> None:
        """The best-effort cache delete must never fail an already-committed
        org-override mutation (the DB write is the source of truth)."""
        org = _org_with_overrides()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger/org-override", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["override"] is True


class TestOrgOverrideRoundTrip:
    def test_get_system_maps_override_is_denied_for_ordinary_caller(
        self, ordinary_internal_flag_client: TestClient
    ) -> None:
        """library.search.in_dev alone does not grant system_maps access."""
        response = ordinary_internal_flag_client.get("/api/v1/admin/feature-flags/system_maps/org-override")
        assert response.status_code == 403

    def test_get_system_maps_override_succeeds_for_internal_caller(self, internal_client: TestClient) -> None:
        org = _org_with_overrides(system_maps=True)
        with patch(
            "modulo.api.routes.admin_feature_flags.get_organisation",
            new_callable=AsyncMock,
            return_value=org,
        ):
            response = internal_client.get("/api/v1/admin/feature-flags/system_maps/org-override")
        assert response.status_code == 200
        assert response.json()["override"] is True

    def test_get_retired_internal_override_is_denied_without_reading(
        self, ordinary_internal_flag_client: TestClient
    ) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags.get_organisation",
            new_callable=AsyncMock,
        ) as get_org:
            response = ordinary_internal_flag_client.get(
                "/api/v1/admin/feature-flags/lifecycle_map_journeys/org-override",
            )
        assert response.status_code == 403
        get_org.assert_not_awaited()

    def test_internal_caller_can_read_retired_override(self, internal_client: TestClient) -> None:
        org = _org_with_overrides(lifecycle_map_journeys=True)
        with patch(
            "modulo.api.routes.admin_feature_flags.get_organisation",
            new_callable=AsyncMock,
            return_value=org,
        ):
            response = internal_client.get(
                "/api/v1/admin/feature-flags/lifecycle_map_journeys/org-override",
            )
        assert response.status_code == 200
        assert response.json()["override"] is True

    def test_set_then_get_org_override_round_trips(self, client: TestClient) -> None:
        """PUT /{flag}/org-override writes the org settings entry and
        GET /{flag}/org-override reads the same persisted value back."""
        org = _org_with_overrides()
        redis_mock = AsyncMock()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            set_resp = client.put("/api/v1/admin/feature-flags/webhook_trigger/org-override", json={"enabled": True})
            assert set_resp.status_code == 200
            assert set_resp.json()["override"] is True
            get_resp = client.get("/api/v1/admin/feature-flags/webhook_trigger/org-override")
        assert get_resp.status_code == 200
        assert get_resp.json()["override"] is True

    def test_toggle_and_org_override_share_persistence_path(self, client: TestClient) -> None:
        """The toggle (PUT /{flag}) and the explicit org-override (PUT
        /{flag}/org-override) write the SAME org settings entry — whichever the
        operator uses, GET /{flag}/org-override must report the persisted value."""
        org = _org_with_overrides()
        redis_mock = AsyncMock()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            toggle_resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
            assert toggle_resp.status_code == 200
            get_resp = client.get("/api/v1/admin/feature-flags/webhook_trigger/org-override")
        assert get_resp.status_code == 200
        assert get_resp.json()["override"] is True


class TestRetiredOrgOverrideAccess:
    def test_clear_system_maps_override_is_denied_for_ordinary_caller(
        self, ordinary_internal_flag_client: TestClient
    ) -> None:
        """library.search.in_dev alone does not grant system_maps access."""
        response = ordinary_internal_flag_client.delete("/api/v1/admin/feature-flags/system_maps/org-override")
        assert response.status_code == 403

    def test_clear_system_maps_override_succeeds_for_internal_caller(self, internal_client: TestClient) -> None:
        org = _org_with_overrides(system_maps=True)
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            response = internal_client.delete("/api/v1/admin/feature-flags/system_maps/org-override")
        assert response.status_code == 200
        assert response.json()["override"] is None

    def test_clear_retired_internal_override_is_denied_without_mutating(
        self, ordinary_internal_flag_client: TestClient
    ) -> None:
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
            ) as get_org,
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                new_callable=AsyncMock,
            ) as update_settings,
        ):
            response = ordinary_internal_flag_client.delete(
                "/api/v1/admin/feature-flags/lifecycle_map_journeys/org-override",
            )
        assert response.status_code == 403
        get_org.assert_not_awaited()
        update_settings.assert_not_awaited()

    def test_internal_caller_can_clear_retired_override(self, internal_client: TestClient) -> None:
        org = _org_with_overrides(lifecycle_map_journeys=True)
        redis_mock = AsyncMock()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                new_callable=AsyncMock,
            ) as update_settings,
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            response = internal_client.delete(
                "/api/v1/admin/feature-flags/lifecycle_map_journeys/org-override",
            )
        assert response.status_code == 200
        assert response.json()["override"] is None
        update_settings.assert_awaited_once()


class TestOrgSettingsPatchScope:
    """FAR-1830: the org-override writers emit ONLY ``feature_overrides`` and
    bind the org RLS context (a revert to a whole-column write would not call
    the patch helper at all)."""

    _ORG_ID = "00000000-0000-0000-0000-000000000001"

    def _capture_patch(self) -> tuple[dict, Any]:
        captured: dict = {}

        async def _capture(session, organisation_id, patch=None, delete_keys=None, **_kwargs):
            captured["patch"] = patch
            return patch or {}

        return captured, _capture

    def test_toggle_patches_only_feature_overrides_and_binds_org(self, client: TestClient) -> None:
        org = _org_with_overrides()
        captured, capture = self._capture_patch()
        bind = AsyncMock()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                new=capture,
            ),
            patch("modulo.api.routes.admin_feature_flags.set_rls_org", bind),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 200
        assert set(captured["patch"]) == {"feature_overrides"}
        assert captured["patch"]["feature_overrides"] == {"webhook_trigger": True}
        assert bind.await_count >= 1
        for call in bind.await_args_list:
            assert str(call.args[1]) == self._ORG_ID

    def test_clear_patches_only_feature_overrides(self, client: TestClient) -> None:
        org = _org_with_overrides(webhook_trigger=True)
        captured, capture = self._capture_patch()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                new=capture,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            resp = client.delete("/api/v1/admin/feature-flags/webhook_trigger/org-override")
        assert resp.status_code == 200
        assert set(captured["patch"]) == {"feature_overrides"}
        assert not captured["patch"]["feature_overrides"]


class TestOrgOverrideOrgIdGuard:
    def test_org_override_rejects_missing_organisation_id(self, no_org_client: TestClient) -> None:
        """A system admin without an organisation_id must be rejected (403) on
        every org-scoped feature-flag override endpoint instead of crashing on
        a None org lookup. Regression for the assert -> if/raise conversion."""
        for method, url in (
            ("get", "/api/v1/admin/feature-flags/sso/org-override"),
            ("put", "/api/v1/admin/feature-flags/sso/org-override"),
            ("delete", "/api/v1/admin/feature-flags/sso/org-override"),
            ("put", "/api/v1/admin/feature-flags/sso"),
        ):
            if method == "get":
                resp = no_org_client.get(url)
            elif method == "put":
                resp = no_org_client.put(url, json={"enabled": True})
            else:
                resp = no_org_client.delete(url)
            assert resp.status_code == 403, (method, resp.status_code, resp.text)
            # The refusal comes from the ROUTE's own org-id guard
            # (``_apply_org_flag_override`` / the handler's explicit check),
            # not from the audit dependency. It used to be the other way
            # round: ``audited(..., principal_dep=get_current_tenant_user)``
            # is solved before the handler and refused any principal without
            # an organisation ("Organisation membership required"), i.e. the
            # audit wrapper acted as an incidental access gate. The principal
            # now resolves without refusing (FAR-1538), so these 403s are the
            # real gate. The un-annotated GET raises the same guard's message.
            # Both are the same 403 org refusal - only the layer differs.
            assert "Organisation" in resp.text


# ---------------------------------------------------------------------------
# Feature-flag override governance audit (feat-org)
# ---------------------------------------------------------------------------
# Org feature-flag overrides change what the whole org can run, so every
# set / clear / toggle must land on the org's tamper-evident audit chain
# (feature_flag_override_set / feature_flag_override_cleared). The audit is
# fail-open: a broken append must never roll back the already-committed
# override, and a raising audit helper must never fail the request.


class TestOrgFlagOverrideAudit:
    # Matches the ``client`` fixture's principal.organisation_id (a string).
    _ORG_ID = "00000000-0000-0000-0000-000000000001"

    def _assert_set_audit(self, mock: AsyncMock, *, flag_name: str, enabled: bool) -> None:
        mock.assert_awaited_once()
        call = mock.await_args
        kwargs = call.kwargs
        assert kwargs["event_type"] == "feature_flag_override_set"
        assert kwargs["resource_type"] == "org"
        assert kwargs["resource_id"] == self._ORG_ID
        assert kwargs["payload"] == {"flag_name": flag_name, "enabled": enabled}
        assert kwargs["log_key"] == "feature_flags.audit_append_failed"
        assert call.args[1].organisation_id == self._ORG_ID
        assert call.args[1].org_role == "admin"
        assert call.args[1].is_system_admin is True

    @contextmanager
    def _audit_env(self, *, org: MagicMock, audit: AsyncMock) -> Generator[None, None, None]:
        """Patch the route collaborators shared by every emission test."""
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
                new=audit,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            yield

    def test_toggle_emits_override_set_audit(self, client: TestClient) -> None:
        audit = AsyncMock()
        with self._audit_env(org=_org_with_overrides(), audit=audit):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["overridden"] is True
        self._assert_set_audit(audit, flag_name="webhook_trigger", enabled=True)

    def test_toggle_off_emits_override_set_audit(self, client: TestClient) -> None:
        audit = AsyncMock()
        with self._audit_env(org=_org_with_overrides(), audit=audit):
            resp = client.put("/api/v1/admin/feature-flags/eval_system", json={"enabled": False})
        assert resp.status_code == 200
        self._assert_set_audit(audit, flag_name="eval_system", enabled=False)

    def test_set_org_override_emits_override_set_audit(self, client: TestClient) -> None:
        audit = AsyncMock()
        with self._audit_env(org=_org_with_overrides(), audit=audit):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger/org-override", json={"enabled": True})
        assert resp.status_code == 200
        self._assert_set_audit(audit, flag_name="webhook_trigger", enabled=True)

    def test_clear_org_override_emits_override_cleared_audit(self, client: TestClient) -> None:
        audit = AsyncMock()
        with self._audit_env(org=_org_with_overrides(webhook_trigger=True), audit=audit):
            resp = client.delete("/api/v1/admin/feature-flags/webhook_trigger/org-override")
        assert resp.status_code == 200
        audit.assert_awaited_once()
        call = audit.await_args
        kwargs = call.kwargs
        assert kwargs["event_type"] == "feature_flag_override_cleared"
        assert kwargs["resource_type"] == "org"
        assert kwargs["resource_id"] == self._ORG_ID
        assert kwargs["payload"] == {"flag_name": "webhook_trigger"}
        assert kwargs["log_key"] == "feature_flags.audit_append_failed"

    def test_audit_failure_is_fail_open(self, client: TestClient) -> None:
        """A failing chain write must not fail the already-committed toggle.

        ``_emit_org_flag_override_audit`` delegates the fail-open contract to
        ``append_audit_event_isolated`` (pinned directly in
        tests/unit/audit_logger/test_audit_logger.py), so this test drives the
        real helper: the underlying chain append raises, the helper swallows
        and logs it, and the request still succeeds.
        """
        org = _org_with_overrides()
        with (
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new_callable=AsyncMock,
                side_effect=RuntimeError("audit backend down"),
            ),
            patch("modulo.core.audit_logger.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.audit_logger.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=AsyncMock(),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["overridden"] is True

    async def test_audit_no_op_when_principal_has_no_organisation(self) -> None:
        """No-op (never raise) when the principal carries no organisation.

        The route 403s this principal upstream, but the helper must be safe to
        call directly and must never be the thing that raises.
        """
        audit = AsyncMock()
        principal = AuthenticatedPrincipal(
            username="testuser",
            organisation_id=None,
            account_id="00000000-0000-0000-0000-000000000002",
            org_role="admin",
            is_system_admin=True,
        )
        with patch(
            "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
            new=audit,
        ):
            await _emit_org_flag_override_audit(
                MagicMock(),
                principal,
                flag_name="webhook_trigger",
                enabled=True,
            )
        audit.assert_not_awaited()

    async def test_audit_uses_neutral_role_sentinel_when_claim_absent(self) -> None:
        """A principal without an org_role must not have one invented for RLS.

        ``org_role`` feeds ``set_rls_user_context`` (``app.org_role``) inside
        the audit transaction; a fabricated ``"admin"`` would misrepresent the
        principal. Absent claim maps to the established neutral ``""`` sentinel
        (the same fallback used when minting access tokens in
        ``modulo/auth/jwt.py``).
        """
        audit = AsyncMock()
        principal = AuthenticatedPrincipal(
            username="testuser",
            organisation_id=self._ORG_ID,
            account_id="00000000-0000-0000-0000-000000000002",
            org_role=None,
            is_system_admin=True,
        )
        with patch(
            "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
            new=audit,
        ):
            await _emit_org_flag_override_audit(
                MagicMock(),
                principal,
                flag_name="webhook_trigger",
                enabled=True,
            )
        audit.assert_awaited_once()
        org_role = audit.await_args.args[1].org_role
        assert org_role == ""

    async def test_audit_reraises_cancelled_error(self) -> None:
        """Cancellation must propagate, never be swallowed by the fail-open helper."""
        principal = AuthenticatedPrincipal(
            username="testuser",
            organisation_id=self._ORG_ID,
            account_id="00000000-0000-0000-0000-000000000002",
            org_role="admin",
            is_system_admin=True,
        )
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
                side_effect=asyncio.CancelledError,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _emit_org_flag_override_audit(
                MagicMock(),
                principal,
                flag_name="webhook_trigger",
                enabled=True,
            )


# ---------------------------------------------------------------------------
# Middleware-level error handling
# ---------------------------------------------------------------------------


class TestCatchAllMiddlewareFallback:
    def test_plain_json_on_serialization_failure(self) -> None:
        from modulo.api.middleware.catch_all import _make_500_response

        resp = _make_500_response(None)
        assert resp.status_code == 500
        body = resp.body
        import json

        parsed = json.loads(body)
        assert parsed["detail"] == "An unexpected error occurred"
        assert parsed["type"] == "urn:problem:modulo:internal_error"
        assert parsed["status"] == 500


# ---------------------------------------------------------------------------
# _resolve_tier — license gating for the frontend tier path
# ---------------------------------------------------------------------------
# Regression for the PR #854 review finding: _resolve_tier duplicated the
# 4-step license resolution and returned the bare org.plan_id for team/pro
# with no license check. It now delegates to resolve_plan_context so a bare
# non-community plan_id with no valid signed license resolves to community —
# closing the UI bypass where FeatureGate.vue / featureEnabled() would have
# shown paid features for an unlicensed org.


def _license_data(tier: str = "team", features: list[str] | None = None) -> LicenseData:
    return LicenseData(
        tier=tier,
        features=features or ["sso"],
        expires_at="",
        org_id="test-org",
        raw_payload={},
        raw_key="",
    )


def _valid_validation(tier: str = "team") -> LicenseValidation:
    return LicenseValidation(valid=True, license_data=_license_data(tier))


def _invalid_validation() -> LicenseValidation:
    return LicenseValidation(valid=False, error="bad key")


def _principal(org_id: uuid.UUID | None = None) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=org_id or uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role="admin",
    )


def _session() -> AsyncMock:
    session = AsyncMock()
    configure_rls_preamble(session)
    session.begin = MagicMock(return_value=AsyncMock())
    return session


def _settings(license_key: str = "") -> MagicMock:
    settings = MagicMock()
    settings.modulo_license_key = license_key
    return settings


def _org(plan_id: str, settings_json: dict | None = None) -> MagicMock:
    org = MagicMock()
    # FAR-2060: an org-scoped key's ``org_id`` claim must match the tenant it is
    # applied to; ``_license_data`` claims "test-org".
    org.id = "test-org"
    org.settings_json = settings_json if settings_json is not None else {}
    org.plan_id = plan_id
    return org


async def _resolve_tier_value(org: MagicMock, *, license_key: str = "") -> str:
    """Run _resolve_tier with no org/system license present by default."""
    session = _session()
    with (
        patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org),
        patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
        patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
        patch("modulo.core.license.get_license", return_value=None),
        patch("modulo.core.license.parse_and_verify", return_value=_invalid_validation()),
    ):
        return await _resolve_tier(_settings(license_key), session, _principal())


class TestResolveTierLicensing:
    async def test_team_plan_id_without_license_returns_community(self) -> None:
        """Bare plan_id='team' with NO license must NOT return team tier.

        The old _resolve_tier returned org.plan_id directly (step 4), so the
        UI plan store showed team features for an unlicensed org.
        """
        assert await _resolve_tier_value(_org("team")) == "community"

    async def test_custom_plan_id_without_license_returns_community(self) -> None:
        assert await _resolve_tier_value(_org("custom-plan")) == "community"

    async def test_team_plan_id_with_env_license_returns_team(self) -> None:
        org = _org("team")
        session = _session()
        with (
            patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=_valid_validation(tier="team")),
        ):
            tier = await _resolve_tier(_settings("env-key"), session, _principal())
        assert tier == "team"

    async def test_org_license_key_wins_over_plan_id(self) -> None:
        org = _org("team", settings_json={"license_key": "org-key"})
        session = _session()
        with (
            patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
            patch("modulo.core.license.parse_and_verify", return_value=_valid_validation(tier="team")),
        ):
            tier = await _resolve_tier(_settings(), session, _principal())
        assert tier == "team"

    async def test_community_plan_id_returns_community(self) -> None:
        assert await _resolve_tier_value(_org("community")) == "community"


# ---------------------------------------------------------------------------
# FAR-1733 — env-var key verification for has_key / is_valid
# ---------------------------------------------------------------------------
# The list endpoint's licence block previously (a) counted the env-var key on
# mere PRESENCE for has_license_key and (b) hardcoded is_valid=True. Both now
# require a key that parse_and_verify confirms (signature + expiry).


async def _build_registry_with(settings: Settings, org: MagicMock | None = None) -> FeatureFlagRegistry:
    """Run the real _build_registry with the DB catalog reads stubbed empty.

    The autouse _patch_resolve_tier fixture pins the tier path to "community";
    get_license / verify_license_key run for real against the test keypair.
    """
    session = _session()
    with (
        patch(
            "modulo.api.routes.admin_feature_flags.get_organisation",
            new_callable=AsyncMock,
            return_value=org,
        ),
        patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
        patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
    ):
        return await _build_registry(settings, session, _principal())


class TestBuildRegistryEnvKeyVerification:
    async def test_invalid_env_key_is_not_counted(self) -> None:
        """An env-var key present but failing verification must NOT set has_license_key.

        FAR-1733 prove-the-fix: the old code counted any non-empty env key
        (presence), so this assertion fails against the old code.
        """
        registry = await _build_registry_with(_settings_with_key("not-a-valid-key"))
        assert registry.has_license_key is False

    async def test_expired_env_key_is_not_counted(self) -> None:
        key = _sign_license_payload(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": (datetime.now(UTC) - timedelta(days=LICENCE_GRACE_DAYS + 1)).isoformat(),
                "org_id": "test-org",
            }
        )
        registry = await _build_registry_with(_settings_with_key(key))
        assert registry.has_license_key is False

    async def test_valid_env_key_is_counted(self) -> None:
        registry = await _build_registry_with(_settings_with_key(_valid_license_key()))
        assert registry.has_license_key is True

    async def test_in_memory_license_is_counted(self) -> None:
        stored = LicenseData(
            tier="team",
            features=["sso"],
            expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            org_id="stored-org",
            raw_payload={},
            raw_key="stored-key",
        )
        store_license("stored-key", stored)
        registry = await _build_registry_with(_settings_with_key(""))
        assert registry.has_license_key is True

    async def test_valid_org_key_is_counted(self) -> None:
        """A valid signed org-level key contributes to has_license_key."""
        org = _org("community", settings_json={"license_key": _valid_license_key()})
        registry = await _build_registry_with(_settings_with_key(""), org=org)
        assert registry.has_license_key is True

    async def test_invalid_org_key_is_not_counted(self) -> None:
        """An org key present but failing verification must not be counted."""
        org = _org("community", settings_json={"license_key": "not-a-valid-key"})
        registry = await _build_registry_with(_settings_with_key(""), org=org)
        assert registry.has_license_key is False

    async def test_expired_org_key_is_not_counted(self) -> None:
        key = _sign_license_payload(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": (datetime.now(UTC) - timedelta(days=LICENCE_GRACE_DAYS + 1)).isoformat(),
                "org_id": "test-org",
            }
        )
        org = _org("community", settings_json={"license_key": key})
        registry = await _build_registry_with(_settings_with_key(""), org=org)
        assert registry.has_license_key is False


class TestLicenseIsValid:
    """FAR-1733: the licence block's is_valid reflects a REAL resolution."""

    async def test_no_licence_anywhere_reports_invalid(self) -> None:
        assert await _license_is_valid(_settings_with_key(""), _session(), None) is False

    async def test_invalid_env_key_reports_invalid(self) -> None:
        """Prove-the-fix: the old endpoint hardcoded is_valid=True for any state."""
        assert await _license_is_valid(_settings_with_key("not-a-valid-key"), _session(), None) is False

    async def test_valid_env_key_reports_valid(self) -> None:
        assert await _license_is_valid(_settings_with_key(_valid_license_key()), _session(), None) is True

    async def test_in_memory_license_reports_valid(self) -> None:
        stored = LicenseData(
            tier="team",
            features=["sso"],
            expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            org_id="stored-org",
            raw_payload={},
            raw_key="stored-key",
        )
        store_license("stored-key", stored)
        assert await _license_is_valid(_settings_with_key(""), _session(), None) is True

    async def test_valid_org_key_reports_valid(self) -> None:
        org = _org("community", settings_json={"license_key": _valid_license_key()})
        session = _session()
        org_id = uuid.uuid4()
        with patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org):
            assert await _license_is_valid(_settings_with_key(""), session, org_id) is True

    async def test_invalid_org_key_reports_invalid(self) -> None:
        """An org key that fails verification does not make the licence valid."""
        org = _org("community", settings_json={"license_key": "not-a-valid-key"})
        session = _session()
        org_id = uuid.uuid4()
        with patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org):
            assert await _license_is_valid(_settings_with_key(""), session, org_id) is False

    async def test_org_read_failure_reports_invalid(self) -> None:
        """A failed org read fails the report closed (False) instead of 500ing."""
        session = _session()
        org_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.admin_feature_flags.get_organisation",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            assert await _license_is_valid(_settings_with_key(""), session, org_id) is False

    async def test_org_missing_reports_invalid(self) -> None:
        """An org id whose read returns None has no key to verify — reports False."""
        session = _session()
        org_id = uuid.uuid4()
        with patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=None):
            assert await _license_is_valid(_settings_with_key(""), session, org_id) is False

    async def test_org_settings_not_a_dict_reports_invalid(self) -> None:
        """A non-dict settings_json means the org rung carries no licence key."""
        org = MagicMock()
        org.settings_json = ["not", "a", "dict"]
        session = _session()
        org_id = uuid.uuid4()
        with patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org):
            assert await _license_is_valid(_settings_with_key(""), session, org_id) is False


# ---------------------------------------------------------------------------
# _enforce_team_tier_gate — org-level enable/disable asymmetry
# ---------------------------------------------------------------------------
# The gate is rank-based over the registry's effective tier ranks and only
# rejects ENABLING a flag that outranks the org's current tier. Disabling must
# always be permitted so an org that enabled a paid flag under a team licence
# and later downgraded can still turn it off. Pinned here as a direct unit
# test, independent of the hermetic BDD mock session setup.


class TestEnforceTeamTierGate:
    def test_enable_team_flag_on_community_is_forbidden(self) -> None:
        registry = _mock_registry()
        flag = registry.get_flag("sso")
        assert flag is not None
        with pytest.raises(HTTPException) as exc_info:
            _enforce_team_tier_gate(flag, "sso", registry, enabled=True)
        assert exc_info.value.status_code == 403

    def test_disable_team_flag_on_community_is_allowed(self) -> None:
        registry = _mock_registry()
        flag = registry.get_flag("sso")
        assert flag is not None
        _enforce_team_tier_gate(flag, "sso", registry, enabled=False)

    def test_enable_community_flag_on_community_is_allowed(self) -> None:
        registry = _mock_registry()
        flag = registry.get_flag("webhook_trigger")
        assert flag is not None
        _enforce_team_tier_gate(flag, "webhook_trigger", registry, enabled=True)

    def test_enable_team_flag_on_team_is_allowed(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("sso")
        assert flag is not None
        _enforce_team_tier_gate(flag, "sso", registry, enabled=True)


# ---------------------------------------------------------------------------
# System-admin requirement on GET endpoints
# ---------------------------------------------------------------------------


class TestSystemAdminRequired:
    """Non-system-admin users must get 403 on feature-flag read endpoints."""

    def test_list_feature_flags_403(self, non_sys_admin_client: TestClient) -> None:
        resp = non_sys_admin_client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 403

    def test_get_feature_flag_403(self, non_sys_admin_client: TestClient) -> None:
        resp = non_sys_admin_client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# FAR-1832 QA F10: a bounded lock timeout (55P03) is a 409 on the org-override
# writers, not the generic 503 the SQLAlchemyError arm answers.
# ---------------------------------------------------------------------------


def _lock_timeout_operational() -> OperationalError:
    """An ``OperationalError`` whose driver error carries SQLSTATE 55P03."""

    class _PgError(Exception):
        sqlstate = "55P03"

    return OperationalError("UPDATE organisations ...", {}, _PgError("canceling statement due to lock timeout"))


class TestOrgOverrideLockTimeout:
    """Both org-override writers route through the atomic settings patch, whose
    ``set_mutation_row_lock_timeout`` bound makes 55P03 reachable. Before F10
    the local ``except SQLAlchemyError`` arm collapsed it to a 503 outage reply,
    inviting a client retry storm against the busy lock."""

    def test_set_org_flag_override_lock_timeout_is_409(self, client: TestClient) -> None:
        org = _org_with_overrides()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                side_effect=_lock_timeout_operational(),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger/org-override", json={"enabled": True})
        assert resp.status_code == 409, resp.text
        assert "another change is in progress" in resp.json()["detail"]

    def test_clear_org_flag_override_lock_timeout_is_409(self, client: TestClient) -> None:
        org = _org_with_overrides(webhook_trigger=True)
        with (
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                side_effect=_lock_timeout_operational(),
            ),
        ):
            resp = client.delete("/api/v1/admin/feature-flags/webhook_trigger/org-override")
        assert resp.status_code == 409, resp.text
        assert "another change is in progress" in resp.json()["detail"]

    def test_toggle_feature_flag_lock_timeout_is_409(self, client: TestClient) -> None:
        """FAR-1832 QA F16: the toggle writer shares
        ``_apply_org_flag_override`` → ``_write_org_override`` with the
        org-override writers, so its ``except SQLAlchemyError`` arm must
        classify 55P03 too — pre-F16 it collapsed the conflict to a 503."""
        org = _org_with_overrides()
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                return_value=_mock_registry(),
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
            ),
            patch(
                "modulo.api.routes.admin_feature_flags.update_org_settings_patch",
                side_effect=_lock_timeout_operational(),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/webhook_trigger", json={"enabled": True})
        assert resp.status_code == 409, resp.text
        assert "another change is in progress" in resp.json()["detail"]
