"""Unit tests for the admin feature-flags API endpoint."""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.admin_feature_flags import _resolve_tier
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.core.license import LicenseData, LicenseValidation
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=True,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
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


def _mock_registry() -> FeatureFlagRegistry:
    """Return a FeatureFlagRegistry with hardcoded flags (no DB)."""
    return FeatureFlagRegistry(current_tier="community", has_license_key=False)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags
# ---------------------------------------------------------------------------


class TestListFeatureFlags:
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


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags/{flag_name}
# ---------------------------------------------------------------------------


class TestGetFeatureFlag:
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
        org = _org_with_overrides(parallel_branches=False)
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
            resp = client.get("/api/v1/admin/feature-flags/parallel_branches")
        assert resp.status_code == 200
        # parallel_branches is community-tier (active on the mock registry);
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
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "sso"
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
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
            assert resp.status_code == 200
            body = resp.json()
            assert body["overridden"] is True
            assert body["currently_active"] is True
            # The durable write landed in the org settings (the DB-row proxy).
            assert org.settings_json["feature_overrides"]["sso"] is True
            # Cache invalidated so app-wide readers see the change immediately.
            redis_mock.delete.assert_awaited_once_with("feature-flags:00000000-0000-0000-0000-000000000001")
            # Second request: a fresh build + org-override overlay must agree.
            listing = client.get("/api/v1/admin/feature-flags")
            assert listing.status_code == 200
            sso = next(f for f in listing.json()["flags"] if f["name"] == "sso")
            assert sso["currently_active"] is True

    def test_toggle_off_persists_and_second_request_sees_flag_inactive(self, client: TestClient) -> None:
        """Disabling via toggle must persist too: parallel_branches is active on
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
            resp = client.put("/api/v1/admin/feature-flags/parallel_branches", json={"enabled": False})
            assert resp.status_code == 200
            assert org.settings_json["feature_overrides"]["parallel_branches"] is False
            listing = client.get("/api/v1/admin/feature-flags")
            assert listing.status_code == 200
            flag = next(f for f in listing.json()["flags"] if f["name"] == "parallel_branches")
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
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
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
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
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
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/sso/org-override", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["override"] is True
        redis_mock.delete.assert_awaited_once_with(f"feature-flags:{self._ORG_ID}")

    def test_clear_org_override_invalidates_redis_cache(self, client: TestClient) -> None:
        org = _org_with_overrides(sso=True)
        redis_mock = AsyncMock()
        with (
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
            resp = client.delete("/api/v1/admin/feature-flags/sso/org-override")
        assert resp.status_code == 200
        assert resp.json()["override"] is None
        redis_mock.delete.assert_awaited_once_with(f"feature-flags:{self._ORG_ID}")

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
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            resp = client.put("/api/v1/admin/feature-flags/sso/org-override", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["override"] is True


class TestOrgOverrideRoundTrip:
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
                "modulo.api.routes.admin_feature_flags.Redis.from_url",
                new_callable=MagicMock,
                return_value=redis_mock,
            ),
        ):
            set_resp = client.put("/api/v1/admin/feature-flags/sso/org-override", json={"enabled": True})
            assert set_resp.status_code == 200
            assert set_resp.json()["override"] is True
            get_resp = client.get("/api/v1/admin/feature-flags/sso/org-override")
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
            assert "Organisation ID required" in resp.text


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
    session.begin = MagicMock(return_value=AsyncMock())
    return session


def _settings(license_key: str = "") -> MagicMock:
    settings = MagicMock()
    settings.modulo_license_key = license_key
    return settings


def _org(plan_id: str, settings_json: dict | None = None) -> MagicMock:
    org = MagicMock()
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
