"""Tests for SSO feature gating via require_feature('sso')."""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import (
    _get_engine,
    get_anonymous_plan_context,
    get_db_session,
    get_plan_context,
    get_system_db_session,
)
from modulo.api.main import app
from modulo.api.models.problem import ProblemException
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.plan_stubs import PlanStub

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _signed_licence(*, tier: str, features: list[str], org_id: str = "", days: int = 30) -> tuple[str, str]:
    """Return ``(licence_key, public_key)`` signed by a throwaway Ed25519 keypair.

    Lets a test drive the REAL ``resolve_plan_context`` (signature verification
    included) instead of hand-building a plan double — the gap the security
    review's MAJOR-5 identified.
    """
    import base64
    from datetime import timedelta

    from modulo.core.registry.crypto import generate_keypair, sign_primitive

    keypair = generate_keypair()
    payload = {
        "tier": tier,
        "features": features,
        "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
        "org_id": org_id,
    }
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = (
        base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, keypair["private_key"]))).decode().rstrip("=")
    )
    return f"{payload_b64}.{sig_b64}", keypair["public_key"]


def _settings_without_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
        # These fixtures model an instance with NO tenant row (the mock session
        # returns an empty organisations list). Under the ADR 005 single-org
        # invariant that is "single-org, tenant unidentifiable", which the
        # pre-auth resolver now FAILS CLOSED on (503). Multi-org mode is the
        # honest way to model "no tenant to bind" -> system scope.
        modulo_multi_org_enabled=True,
    )


def _settings_with_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="valid-license-key",
        modulo_multi_org_enabled=True,
    )


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides.clear()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Shape the CRUD read results so resolve_plan_context's catalog reads and
    # the provider listings see empty lists instead of MagicMock iterables.
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


def _build_client(settings_fn, sso_enabled: bool = True) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = settings_fn
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    # An unlicensed Community plan has NO licence key; a licensed plan does.
    # The distinction drives the FAR-2059 degraded-authentication carve-out.
    mock_plan = PlanStub(enabled=sso_enabled, tier="team" if sso_enabled else "community")
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_anonymous_plan_context] = lambda: mock_plan
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_sso() -> Generator[TestClient, None, None]:
    yield from _build_client(_settings_without_license, sso_enabled=False)


@pytest.fixture
def client_with_sso() -> Generator[TestClient, None, None]:
    yield from _build_client(_settings_with_license, sso_enabled=True)


# ── SSO admin endpoints (should return 402 when feature disabled) ────────


class TestAdminSsoGating:
    def test_list_providers_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/admin/sso/providers")
        assert resp.status_code == 402
        assert "not available on your plan" in resp.text.lower()

    def test_list_providers_succeeds_when_enabled(self, client_with_sso: TestClient) -> None:
        mock_provider = MagicMock()
        mock_provider.id = _PROVIDER_ID
        mock_provider.provider_type = "oidc"
        mock_provider.provider_id = "test-oidc"
        mock_provider.name = "Test"
        mock_provider.client_id = "cid"
        mock_provider.client_secret = "secret"
        mock_provider.discovery_url = "https://example.com"
        mock_provider.metadata_url = None
        mock_provider.metadata_xml = None
        mock_provider.entity_id = None
        mock_provider.scopes = json.dumps(["openid"])
        mock_provider.enabled = True
        mock_provider.auto_provision = True
        mock_provider.default_role = "runner"
        mock_provider.group_mappings = []
        mock_provider.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        mock_provider.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

        with patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(return_value=[mock_provider])):
            resp = client_with_sso.get("/api/v1/admin/sso/providers")
            assert resp.status_code == 200

    def test_create_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post(
            "/api/v1/admin/sso/providers",
            json={"provider_type": "oidc", "name": "Test"},
        )
        assert resp.status_code == 402

    def test_update_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.put(
            f"/api/v1/admin/sso/providers/{_PROVIDER_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 402

    def test_delete_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.delete(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}")
        assert resp.status_code == 402

    def test_toggle_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
        assert resp.status_code == 402

    def test_test_connection_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
        assert resp.status_code == 402

    def test_set_group_mappings_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.put(
            f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
            json={"mappings": []},
        )
        assert resp.status_code == 402

    def test_get_group_mappings_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings")
        assert resp.status_code == 402


# ── SSO auth login/callback endpoints ────────────────────────────────────
#
# NOTE: /api/v1/auth/sso/providers is a PRE-AUTH discovery endpoint (the login
# page fetches it before any user exists). It no longer sits behind
# require_feature/get_current_user, and on an unlicensed instance it advertises
# the configured providers so an SSO-only deployment can still sign in
# (FAR-2059). The listing is suppressed only when a licence resolves but does
# not grant SSO — a determinate downgrade, where the auth gate would 402.
#
# FAR-2059: the login flows are authentication entry points. With no valid
# licence they proceed in degraded mode instead of returning 402; entitlement
# is enforced post-auth (Community plan -> Team features off).


class _DeterminatePlan:
    """Plan double with separate feature and licence-key state.

    A licence that RESOLVES but does not grant a feature is a determinate
    entitlement decision (402). An unlicensed plan has no key at all and opts
    authentication entry points into degraded mode.
    """

    def __init__(self, *, enabled: bool, has_license_key: bool) -> None:
        self._enabled = enabled
        self._has_license_key = has_license_key

    def feature_enabled(self, name: str) -> bool:
        return self._enabled

    def list_enabled_features(self) -> list[str]:
        return []

    def tier(self) -> str:
        return "team" if self._has_license_key else "community"

    def has_license_key(self) -> bool:
        return self._has_license_key


def _env_oidc_settings(*, license_key: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=license_key,
        # No tenant row in the mock session -> model "no tenant to bind" as
        # multi-org (system scope) rather than the fail-closed single-org state.
        modulo_multi_org_enabled=True,
        modulo_oidc_providers=json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "cid",
                    "client_secret": "secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                }
            ]
        ),
    )


def _client_with_anonymous_plan(settings: Settings, plan: object) -> TestClient:
    """TestClient with a mock session and the given anonymous plan override."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_anonymous_plan_context] = lambda: plan
    return TestClient(app, follow_redirects=False)


class TestSsoAuthGating:
    def test_sso_providers_returns_200_empty_when_no_providers_configured(self, client_no_sso: TestClient) -> None:
        """Nothing configured -> normal 200 with an empty providers list (no 402/401)."""
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
            resp = client_no_sso.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json() == {"oidc": [], "saml": False}

    def test_sso_providers_needs_no_authentication(self) -> None:
        """Anonymous request with the REAL auth dependencies in place gets a 200.

        Regression guard: require_feature used to pull get_current_user into
        this route, which 401'd the pre-auth login-page call.
        """
        app.dependency_overrides.clear()
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _settings_without_license
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_system_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        # get_current_user / get_plan_context deliberately NOT overridden: the
        # real dependencies would run for any request. The route must never
        # consult them.
        try:
            client = TestClient(app)
            with (
                patch("modulo.core.license.get_license", return_value=None),
                patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
            ):
                resp = client.get("/api/v1/auth/sso/providers")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert resp.json() == {"oidc": [], "saml": False}

    async def test_anonymous_plan_resolution_failure_fails_closed(self, caplog: pytest.LogCaptureFixture) -> None:
        """A resolver failure must FAIL CLOSED — it must never yield an allow.

        Regression guard for the security review's MAJOR-1: the old fallback
        returned ``CommunityTier()``, whose ``has_license_key() is False`` made
        the degraded-authentication branch ALLOW — turning a malformed
        ``tier_catalog`` row into an authentication bypass. A resolution
        failure is now a 5xx (deny), distinct from "no licence resolved".
        """
        from fastapi import HTTPException

        from modulo.api.dependencies import resolve_anonymous_plan_context

        broken_session = MagicMock()

        def _begin_raises() -> None:
            raise TypeError("test-double session does not support begin()")

        broken_session.begin.side_effect = _begin_raises

        with (
            caplog.at_level("ERROR", logger="modulo.api.dependencies"),
            pytest.raises(HTTPException) as excinfo,
        ):
            await resolve_anonymous_plan_context(_settings_without_license(), broken_session)

        assert excinfo.value.status_code == 503
        assert any("anonymous_plan_context_resolution_failed" in rec.message for rec in caplog.records), (
            "a resolution failure must be logged, not swallowed"
        )

    async def test_single_org_with_no_identifiable_tenant_fails_closed(self) -> None:
        """Single-org invariant + ambiguous tenant -> 5xx, never system scope."""
        from fastapi import HTTPException

        from modulo.api.dependencies import resolve_anonymous_plan_context

        app.dependency_overrides.clear()
        session = _make_mock_session()
        settings = _settings_without_license().model_copy(update={"modulo_multi_org_enabled": False})

        with pytest.raises(HTTPException) as excinfo:
            await resolve_anonymous_plan_context(settings, session)

        assert excinfo.value.status_code == 503

    def test_resolution_failure_is_never_an_allow_at_the_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MAJOR-1 end-to-end: a resolution bug must not open an auth route.

        A malformed ``tier_catalog`` row raises ``TypeError`` deep inside
        ``_load_catalog``. The old fallback turned that into ``CommunityTier``
        with ``has_license_key() == False``, which the degraded-authentication
        branch read as "no licence" and ALLOWED. The route must now 5xx.
        """
        app.dependency_overrides.clear()
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        async def _malformed_catalog(*_args: object, **_kwargs: object) -> object:
            raise TypeError("string indices must be integers")

        app.dependency_overrides[get_settings] = _settings_without_license
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_system_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        monkeypatch.setattr("modulo.core.feature_flags.resolve_plan_context", _malformed_catalog)
        try:
            client = TestClient(app, follow_redirects=False)
            resp = client.get("/api/v1/auth/oidc/google/login")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code >= 500, resp.text
        assert resp.status_code != 402, "a resolution failure is NOT a licence verdict"

    def test_sso_providers_lists_env_providers_when_enabled(self, client_with_sso: TestClient) -> None:
        """SSO licensed/enabled -> 200 with the configured OIDC providers."""
        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key="valid-license-key",
            modulo_multi_org_enabled=True,
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "google",
                        "client_id": "cid",
                        "client_secret": "secret",
                        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                    }
                ]
            ),
        )
        app.dependency_overrides[get_settings] = lambda: settings
        licensed = SimpleNamespace(tier="team", features=["sso"], expires_at=None)
        with (
            patch("modulo.core.license.get_license", return_value=licensed),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
            resp = client_with_sso.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert "google" in [p["provider_id"] for p in body["oidc"]]

    # ── FAR-2059: no licence -> authentication proceeds (degraded) ──────────
    #
    # These replace the old "unlicensed -> 402" assertions: that 402 WAS the
    # lockout. The contract is now "the gate does not block sign-in"; the route
    # fails only on its own configuration state.

    def test_oidc_login_proceeds_when_unlicensed(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/oidc/google/login")
        assert resp.status_code != 402, resp.text
        # The gate let it through; no IdP is configured in this fixture.
        assert resp.status_code == 400

    def test_oidc_callback_proceeds_when_unlicensed(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/oidc/google/callback?code=abc&state=def")
        assert resp.status_code != 402, resp.text
        # Browser-facing failure -> login page, never a licence gate.
        assert resp.status_code == 303

    def test_saml_login_proceeds_when_unlicensed(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/saml/login")
        assert resp.status_code != 402, resp.text
        assert resp.status_code == 400

    def test_saml_acs_proceeds_when_unlicensed(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post("/api/v1/auth/saml/acs", data={"SAMLResponse": "dGVzdA=="})
        assert resp.status_code != 402, resp.text
        assert resp.status_code == 303

    def test_saml_metadata_proceeds_when_unlicensed(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/saml/metadata")
        assert resp.status_code != 402, resp.text
        assert resp.status_code == 400

    # FAR-2059: the three PER-PROVIDER SAML routes each carry their own
    # ``allow_degraded_authentication`` opt-in. They previously had no
    # assertion either way, so deleting the opt-in from any one of them
    # failed no test. Drive all three.
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/v1/auth/saml/acs/provider-x"),
            ("get", "/api/v1/auth/saml/provider-x/metadata"),
            ("get", "/api/v1/auth/saml/provider-x/login"),
        ],
    )
    def test_per_provider_saml_routes_proceed_when_unlicensed(
        self, client_no_sso: TestClient, method: str, path: str
    ) -> None:
        resp = (
            client_no_sso.post(path, data={"SAMLResponse": "dGVzdA=="}) if method == "post" else client_no_sso.get(path)
        )
        assert resp.status_code not in (401, 402), (
            f"{path} must not licence-gate an unauthenticated sign-in: {resp.status_code} {resp.text}"
        )

    # ── FAR-2060: a determinate refusal still 402s and names the scope ──────

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/auth/oidc/google/login"),
            ("get", "/api/v1/auth/oidc/google/callback?code=abc&state=def"),
            ("get", "/api/v1/auth/saml/login"),
            ("post", "/api/v1/auth/saml/acs"),
            ("get", "/api/v1/auth/saml/metadata"),
            ("post", "/api/v1/auth/saml/acs/provider-x"),
            ("get", "/api/v1/auth/saml/provider-x/metadata"),
            ("get", "/api/v1/auth/saml/provider-x/login"),
        ],
        ids=[
            "oidc-login",
            "oidc-callback",
            "saml-login",
            "saml-acs",
            "saml-metadata",
            "saml-acs-provider",
            "saml-metadata-provider",
            "saml-login-provider",
        ],
    )
    def test_licensed_without_sso_still_402_names_scope(self, method: str, path: str) -> None:
        """A licence that RESOLVES but lacks sso is a determinate refusal.

        The 402 detail must tell the operator which licence scope is required,
        instead of the old unexplained "not available on your plan".
        """
        plan = _DeterminatePlan(enabled=False, has_license_key=True)
        client = _client_with_anonymous_plan(_settings_with_license(), plan)
        try:
            resp = client.post(path, data={"SAMLResponse": "dGVzdA=="}) if method == "post" else client.get(path)
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 402, resp.text
        assert "system or organisation scope" in resp.text

    # ── FAR-2059/2060: discovery visibility ────────────────────────────────

    def test_sso_providers_listed_when_unlicensed(self) -> None:
        """No valid licence -> configured providers stay visible so SSO-only login works.

        The discovery route resolves the REAL anonymous plan (it calls the
        resolver directly, not the DI dependency), so this drives the real
        unlicensed fallback with an env-configured provider.
        """
        client = _client_with_anonymous_plan(_env_oidc_settings(), PlanStub(enabled=False, tier="community"))
        try:
            with (
                patch("modulo.core.license.get_license", return_value=None),
                patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
            ):
                resp = client.get("/api/v1/auth/sso/providers")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "google" in [p["provider_id"] for p in resp.json()["oidc"]]

    def test_sso_providers_hidden_when_licensed_without_sso(self) -> None:
        """A licence that resolves without sso hides the listing (the gate would 402)."""
        determinate = _DeterminatePlan(enabled=False, has_license_key=True)
        client = _client_with_anonymous_plan(_env_oidc_settings(license_key="valid-key"), determinate)
        try:
            with patch("modulo.core.feature_flags.resolve_plan_context", AsyncMock(return_value=determinate)):
                resp = client.get("/api/v1/auth/sso/providers")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert resp.json() == {"oidc": [], "saml": False}


# ── MAJOR-5: prove the gate through the REAL resolution ladder ────────────


class TestRealLicenceResolution:
    """The central property must hold against real signature verification.

    The other gate tests use a hand-built ``_DeterminatePlan`` double, so a
    regression inside ``_resolve_paid_plan`` (e.g. treating a valid Community
    key as "no licence") would pass silently. These drive the real
    ``resolve_plan_context`` with a real signed key.
    """

    async def test_real_community_key_without_sso_still_402s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid-but-SSO-less key is a DETERMINATE refusal, never degraded.

        The FAR-2059 carve-out is "no licence resolved". A signed Community key
        resolves but does not grant ``sso``, so the gate must still 402 — and
        the boundary is left exactly as-is (a policy question for Duncan).
        """
        from modulo.api.dependencies import require_feature_anonymous, resolve_anonymous_plan_context
        from modulo.core import license as license_mod
        from modulo.core.feature_flags import FeatureFlagRegistry

        key, public_key = _signed_licence(tier="community", features=[])
        monkeypatch.setattr(license_mod, "_configured_public_key_hex", public_key)
        monkeypatch.setattr(license_mod, "_current_license", None)
        monkeypatch.setattr(FeatureFlagRegistry, "_overrides", {})

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key=key,
            # Empty org list models "no tenant to bind"; multi-org is the
            # honest representation of that state (system scope).
            modulo_multi_org_enabled=True,
        )

        ctx = await resolve_anonymous_plan_context(settings, _make_mock_session())

        assert ctx.has_license_key() is True
        assert ctx.feature_enabled("sso") is False

        gate = require_feature_anonymous("sso", allow_degraded_authentication=True)
        with pytest.raises(ProblemException) as excinfo:
            gate.dependency(ctx=ctx)
        assert excinfo.value.status_code == 402

    async def test_real_team_key_grants_the_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: the same real ladder with SSO in the claims PASSES the gate."""
        from modulo.api.dependencies import require_feature_anonymous, resolve_anonymous_plan_context
        from modulo.core import license as license_mod
        from modulo.core.feature_flags import FeatureFlagRegistry

        key, public_key = _signed_licence(tier="team", features=["sso"])
        monkeypatch.setattr(license_mod, "_configured_public_key_hex", public_key)
        monkeypatch.setattr(license_mod, "_current_license", None)
        monkeypatch.setattr(FeatureFlagRegistry, "_overrides", {})

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key=key,
            modulo_multi_org_enabled=True,
        )

        ctx = await resolve_anonymous_plan_context(settings, _make_mock_session())

        assert ctx.has_license_key() is True
        assert ctx.feature_enabled("sso") is True
        gate = require_feature_anonymous("sso", allow_degraded_authentication=True)
        assert gate.dependency(ctx=ctx) is None

    async def test_team_key_inside_delivery_grace_still_grants_sso(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-2059 grace boundary: an expiry 13 days ago still retains Team."""
        from modulo.api.dependencies import require_feature_anonymous, resolve_anonymous_plan_context
        from modulo.core import license as license_mod
        from modulo.core.feature_flags import FeatureFlagRegistry
        from modulo.core.license import LICENCE_GRACE_DAYS

        key, public_key = _signed_licence(tier="team", features=["sso"], days=-(LICENCE_GRACE_DAYS - 1))
        monkeypatch.setattr(license_mod, "_configured_public_key_hex", public_key)
        monkeypatch.setattr(license_mod, "_current_license", None)
        monkeypatch.setattr(FeatureFlagRegistry, "_overrides", {})

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key=key,
            modulo_multi_org_enabled=True,
        )

        ctx = await resolve_anonymous_plan_context(settings, _make_mock_session())

        assert ctx.has_license_key() is True
        assert ctx.tier() == "team"
        assert ctx.feature_enabled("sso") is True
        gate = require_feature_anonymous("sso", allow_degraded_authentication=True)
        assert gate.dependency(ctx=ctx) is None

    async def test_team_key_past_delivery_grace_falls_to_community(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other side: an expiry 15 days ago grants no Team capability.

        The key no longer resolves, so the pre-auth entry point degrades
        (authentication is not blocked) but the resolved plan is Community and
        every Team feature is off post-auth.
        """
        from modulo.api.dependencies import require_feature_anonymous, resolve_anonymous_plan_context
        from modulo.core import license as license_mod
        from modulo.core.feature_flags import FeatureFlagRegistry
        from modulo.core.license import LICENCE_GRACE_DAYS

        key, public_key = _signed_licence(tier="team", features=["sso"], days=-(LICENCE_GRACE_DAYS + 1))
        monkeypatch.setattr(license_mod, "_configured_public_key_hex", public_key)
        monkeypatch.setattr(license_mod, "_current_license", None)
        monkeypatch.setattr(FeatureFlagRegistry, "_overrides", {})

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key=key,
            modulo_multi_org_enabled=True,
        )

        ctx = await resolve_anonymous_plan_context(settings, _make_mock_session())

        assert ctx.has_license_key() is False
        assert ctx.tier() == "community"
        assert ctx.feature_enabled("sso") is False
        # Identity exchange is NOT blocked: the degraded branch allows it.
        gate = require_feature_anonymous("sso", allow_degraded_authentication=True)
        assert gate.dependency(ctx=ctx) is None


# ── Non-SSO endpoints should be unaffected ──


class TestNonSsoEndpoints:
    def test_health_returns_200(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/health")
        assert resp.status_code in (200, 404)

    def test_login_returns_422_without_body(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post("/api/v1/auth/login", json={})
        assert resp.status_code == 422


# ── FAR-847: Pre-auth SSO routes must not require authentication ──────────
#
# Regression guard: the five SSO login/callback/SAML routes are authentication
# entry points — a browser navigating from the logged-out login page never
# carries a bearer token.  Before this fix, require_feature("sso") pulled
# get_current_user into the dependency chain, which raised 401 "Not
# authenticated" BEFORE the route handler executed.  The fix replaces
# require_feature with require_feature_anonymous, which resolves the plan
# WITHOUT get_current_user.


class TestPreAuthSsoNoAuthRequired:
    """Pre-auth SSO routes must never 401 AND never licence-lock-out sign-in.

    FAR-847: these are authentication entry points, so they must not require a
    bearer token. FAR-2059: on an instance with NO valid licence they must not
    answer 402 either — the gate lets authentication proceed in degraded mode
    (Team features stay off post-auth). The route then fails only on its own
    configuration state: no IdP configured -> 400, browser-facing callback
    failures -> a 303 redirect to the login page.

    These tests exercise the REAL dependency chain (no get_current_user or
    get_plan_context overrides) with a mock session so
    resolve_anonymous_plan_context can run and resolve to the unlicensed
    Community plan.
    """

    def _build_anon_client(self) -> TestClient:
        """Build a TestClient with real auth deps but a mock DB session.

        Does NOT override get_current_user or get_plan_context: those
        dependencies must never be consulted by the pre-auth routes.
        ``follow_redirects=False`` keeps the browser-facing 303 observable.
        """
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _settings_without_license
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_system_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        # get_current_user deliberately NOT overridden — if the route
        # consults it, the request will 401 and the test fails.
        return TestClient(app, follow_redirects=False)

    def test_oidc_login_no_auth_not_401_or_402(self) -> None:
        """GET /oidc/{provider}/login: no auth (not 401), no licence (not 402)."""
        try:
            client = self._build_anon_client()
            resp = client.get("/api/v1/auth/oidc/google/login")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code != 401, f"Pre-auth OIDC login must not require authentication, got 401: {resp.text}"
        assert resp.status_code != 402, f"An unlicensed instance must not lock SSO sign-in out, got 402: {resp.text}"
        # No IdP is configured in this fixture, so the handler's own check fires.
        assert resp.status_code == 400

    def test_oidc_callback_no_auth_not_401_or_402(self) -> None:
        """GET /oidc/{provider}/callback: browser-facing failure -> 303, never 401/402."""
        try:
            client = self._build_anon_client()
            resp = client.get("/api/v1/auth/oidc/google/callback")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code != 401, f"Pre-auth OIDC callback must not require authentication, got 401: {resp.text}"
        assert resp.status_code != 402, f"An unlicensed instance must not lock SSO sign-in out, got 402: {resp.text}"
        assert resp.status_code == 303

    def test_saml_login_no_auth_not_401_or_402(self) -> None:
        """GET /saml/login: no auth (not 401), no licence (not 402)."""
        try:
            client = self._build_anon_client()
            resp = client.get("/api/v1/auth/saml/login")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code != 401, f"Pre-auth SAML login must not require authentication, got 401: {resp.text}"
        assert resp.status_code != 402, f"An unlicensed instance must not lock SSO sign-in out, got 402: {resp.text}"
        assert resp.status_code == 400

    def test_saml_acs_no_auth_not_401_or_402(self) -> None:
        """POST /saml/acs: browser-facing failure -> 303, never 401/402."""
        try:
            client = self._build_anon_client()
            resp = client.post("/api/v1/auth/saml/acs", data={"SAMLResponse": "dGVzdA=="})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code != 401, f"Pre-auth SAML ACS must not require authentication, got 401: {resp.text}"
        assert resp.status_code != 402, f"An unlicensed instance must not lock SSO sign-in out, got 402: {resp.text}"
        assert resp.status_code == 303

    def test_saml_metadata_no_auth_not_401_or_402(self) -> None:
        """GET /saml/metadata: no auth (not 401), no licence (not 402)."""
        try:
            client = self._build_anon_client()
            resp = client.get("/api/v1/auth/saml/metadata")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code != 401, f"Pre-auth SAML metadata must not require authentication, got 401: {resp.text}"
        assert resp.status_code != 402, f"An unlicensed instance must not lock SSO sign-in out, got 402: {resp.text}"
        assert resp.status_code == 400


class TestAuthenticatedRoutesStillGuarded:
    """Control: authenticated-only routes MUST still 401 without credentials.

    Proves the gate was not globally weakened — only the five pre-auth SSO
    routes use require_feature_anonymous; admin SSO routes keep the
    authenticated require_feature dependency.
    """

    def test_admin_sso_list_providers_401_without_auth(self) -> None:
        """GET /admin/sso/providers without auth must 401 (not 200/402).

        The admin SSO list endpoint is behind the standard
        require_feature("sso") which chains to get_current_user.  Without
        a bearer token it must 401, proving the anonymous gate was NOT
        applied to authenticated routes.
        """
        app.dependency_overrides.clear()
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _settings_with_license
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_system_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        # Do NOT override get_current_user: the real dependency must 401.
        try:
            client = TestClient(app)
            resp = client.get("/api/v1/admin/sso/providers")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 401, (
            f"Admin SSO route must still require authentication, got {resp.status_code}: {resp.text}"
        )


class TestOidcLoginPublicUrlBridge:
    """FAR-1159 prove-the-fix: the OIDC login redirect is anchored to the
    effective MODULO_PUBLIC_URL, so a runtime override wins over Settings.

    This exercises the handler body (SSO enabled), which the 402/401 gating
    tests above never reach, and asserts the redirect_uri is built from the
    override — reverting the route to ``settings.modulo_public_url`` fails it.
    """

    def test_oidc_login_redirect_uri_uses_public_url_override(self, client_with_sso: TestClient) -> None:
        from modulo.core.runtime_config.store import RuntimeConfigStore, get_runtime_config_store

        captured: dict[str, str] = {}

        async def _fake_authorize_url(
            provider: str,
            settings: Settings,
            redirect_uri: str,
            system_session: AsyncMock,
            session: AsyncMock,
        ) -> tuple[str, str, str, str]:
            captured["redirect_uri"] = redirect_uri
            # FAR-1982: real contract — (url, raw_state, nonce, code_verifier).
            return "https://idp.example.com/authorize", "raw-state", "nonce", "verifier"

        RuntimeConfigStore.reset()
        store = get_runtime_config_store()
        store.set_override("MODULO_PUBLIC_URL", "https://hot.example.com")
        try:
            with patch(
                "modulo.api.routes.sso.oidc_get_authorize_url",
                new=AsyncMock(side_effect=_fake_authorize_url),
            ):
                resp = client_with_sso.get("/api/v1/auth/oidc/google/login", follow_redirects=False)
        finally:
            store.clear_override("MODULO_PUBLIC_URL")
            RuntimeConfigStore.reset()

        assert resp.status_code == 307, resp.text
        assert captured["redirect_uri"] == "https://hot.example.com/api/v1/auth/oidc/google/callback"
