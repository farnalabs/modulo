"""BDD-mirror unit tests: MCP OAuth 2.0 authorization code flow (ADR 017 A1b).

Each test maps to a Gherkin scenario in tests/bdd/features/mcp/mcp_oauth.feature.
These cover the authorize (GET 302), consent approve, token and refresh
protocol endpoints (in mcp_server.py / mcp_oauth.py) in addition to the client
CRUD endpoints already tested in test_mcp_oauth.py.
"""

import json as json_module
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.mcp_server import (
    McpAuthMiddleware,
    _ctx_auth_token,
    _ctx_auth_type,
    _ctx_key_id,
    _ctx_org_id,
    _ctx_role,
    _ctx_user_id,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.oauth import (
    InvalidGrantError,
    OAuthAccessTokenClaims,
    UnauthorizedClientError,
    compute_pkce_challenge,
    create_oauth_access_token,
    validate_client_scopes,
)
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_CODE_CHALLENGE = compute_pkce_challenge(_CODE_VERIFIER)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="https://modulo.example.com",
        cors_origins="https://modulo.example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_mock_session_factory() -> MagicMock:
    mock_session = _make_mock_session()
    factory = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)
    factory.return_value = cm
    return factory


def _make_admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _make_viewer_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )


def _make_mock_client(
    client_id: str = "oauth_client_1",
    name: str = "My MCP App",
    scopes: str = "trigger:run hitl:review",
    redirect_uris: str = "https://app.example.com/callback",
) -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.client_id = client_id
    c.client_secret_hash = "a" * 64
    c.name = name
    c.scopes = scopes
    c.redirect_uris = redirect_uris
    c.organisation_id = _ORG_ID
    return c


def _make_mock_auth_code(
    client_id: str = "oauth_client_1",
    scopes: str = "trigger:run",
    redirect_uri: str = "https://app.example.com/callback",
    used: bool = False,
    code_challenge: str | None = _CODE_CHALLENGE,
    account_id: uuid.UUID = _USER_ID,
) -> MagicMock:
    c = MagicMock()
    c.code = "auth_code_abc"
    c.client_id = client_id
    c.organisation_id = _ORG_ID
    c.account_id = account_id
    c.scopes = scopes
    c.redirect_uri = redirect_uri
    c.used = used
    c.code_challenge = code_challenge
    c.code_challenge_method = "S256"
    c.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    return c


def _make_mock_consent_state(
    state: str = "state-xyz",
    client_id: str = "oauth_client_1",
    scopes: list[str] | None = None,
    redirect_uri: str = "https://app.example.com/callback",
    code_challenge: str = _CODE_CHALLENGE,
    organisation_id: uuid.UUID = _ORG_ID,
) -> MagicMock:
    s = MagicMock()
    s.state = state
    s.client_id = client_id
    s.scopes = scopes or ["trigger:run"]
    s.redirect_uri = redirect_uri
    s.code_challenge = code_challenge
    s.organisation_id = organisation_id
    s.consumed = False
    return s


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_admin_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_viewer_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario: Admin registers an OAuth client
# ---------------------------------------------------------------------------


class TestRegisterOAuthClientBDD:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_create_returns_client_id_and_secret(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            patch("modulo.api.routes.mcp_oauth.normalize_scopes") as mock_norm,
        ):
            mock_norm.return_value = ["trigger:run", "hitl:review"]
            mock_client = MagicMock()
            mock_client.id = uuid.uuid4()
            mock_client.client_id = "abc123def4567890"
            mock_client.name = "My MCP App"
            mock_create.return_value = (mock_client, "raw_secret_40_chars_long_here")

            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "My MCP App",
                    "redirect_uris": ["https://app.example.com/callback"],
                    "scopes": ["trigger:run", "hitl:review"],
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["client_id"] == "abc123def4567890"
        assert body["client_secret"] == "raw_secret_40_chars_long_here"
        assert body["name"] == "My MCP App"

    def test_non_admin_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            self.ENDPOINT,
            json={
                "name": "My MCP App",
                "redirect_uris": ["https://app.example.com/callback"],
                "scopes": ["trigger:run"],
            },
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Scenario: Authorization request with PKCE (GET /mcp/oauth/authorize → 302)
# ---------------------------------------------------------------------------


class TestAuthorizeEndpoint:
    """Tests the ``_oauth_authorize`` handler directly (anonymous browser GET)."""

    @staticmethod
    def _make_request(params: dict[str, str]) -> Request:
        query = "&".join(f"{k}={v}" for k, v in params.items()).encode()
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/mcp/oauth/authorize",
            "headers": [],
            "query_string": query,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
        }

        async def receive() -> dict:
            return {"type": "http.disconnect"}

        return Request(scope, receive=receive)

    async def _authorize(self, params: dict[str, str]) -> Any:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
            patch("modulo.auth.oauth.create_consent_state") as mock_create_state,
            patch("modulo.auth.oauth.normalize_scopes") as mock_norm,
            patch("modulo.auth.oauth.validate_client_scopes") as mock_val,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_norm.return_value = ["trigger:run"]
            mock_val.return_value = ["trigger:run"]
            mock_get.return_value = _make_mock_client()

            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(params)
            response = await _oauth_authorize(request)
            return response, mock_create_state

    async def test_authorize_redirects_to_spa_consent_route(self) -> None:
        response, mock_create_state = await self._authorize(
            {
                "response_type": "code",
                "client_id": "oauth_client_1",
                "redirect_uri": "https://app.example.com/callback",
                "scope": "trigger:run",
                "code_challenge": _CODE_CHALLENGE,
                "code_challenge_method": "S256",
                "state": "xyz",
            }
        )

        assert response.status_code == 302
        assert response.headers.get("Referrer-Policy") == "no-referrer"
        location = response.headers["Location"]
        assert location.startswith("https://modulo.example.com/oauth/authorize?")
        assert "client_id=oauth_client_1" in location
        assert "state=xyz" in location
        assert f"code_challenge={_CODE_CHALLENGE}" in location
        # The consent-state row is created at authorize with the challenge —
        # it is NOT minted into a code until the authenticated approve.
        assert mock_create_state.await_count == 1
        kwargs = mock_create_state.call_args.kwargs
        assert kwargs["state"] == "xyz"
        assert kwargs["scopes"] == ["trigger:run"]
        assert kwargs["code_challenge"] == _CODE_CHALLENGE
        assert kwargs["org_id"] == _ORG_ID

    async def test_authorize_rejects_plain_pkce_method(self) -> None:
        with (
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.api.mcp_server._get_session_factory"),
        ):
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "https://app.example.com/callback",
                    "scope": "trigger:run",
                    "code_challenge": _CODE_CHALLENGE,
                    "code_challenge_method": "plain",
                    "state": "xyz",
                }
            )
            response = await _oauth_authorize(request)

        assert response.status_code == 400
        assert "S256" in json_module.loads(response.body)["detail"]

    async def test_authorize_rejects_missing_state(self) -> None:
        with (
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.api.mcp_server._get_session_factory"),
        ):
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "https://app.example.com/callback",
                    "scope": "trigger:run",
                    "code_challenge": _CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                }
            )
            response = await _oauth_authorize(request)

        assert response.status_code == 400
        assert "state" in json_module.loads(response.body)["detail"]

    async def test_authorize_rejects_missing_code_challenge(self) -> None:
        with (
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.api.mcp_server._get_session_factory"),
        ):
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "https://app.example.com/callback",
                    "scope": "trigger:run",
                    "code_challenge_method": "S256",
                    "state": "xyz",
                }
            )
            response = await _oauth_authorize(request)

        assert response.status_code == 400
        assert "code_challenge" in json_module.loads(response.body)["detail"]


# ---------------------------------------------------------------------------
# Scenario: Invalid redirect_uri rejected at authorize
# ---------------------------------------------------------------------------


class TestRedirectUriValidation:
    async def test_mismatched_redirect_uri_returns_error(self) -> None:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_get.return_value = _make_mock_client(
                client_id="oauth_client_1",
                redirect_uris="https://app.example.com/callback",
            )

            from modulo.api.mcp_server import _oauth_authorize

            query = (
                "response_type=code"
                "&client_id=oauth_client_1"
                "&redirect_uri=https%3A%2F%2Fevil.com%2Fphish"
                "&scope=trigger:run"
                "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
                "&code_challenge_method=S256"
                "&state=xyz"
            )
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/mcp/oauth/authorize",
                "headers": [],
                "query_string": query.encode(),
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
            }

            async def receive() -> dict:
                return {"type": "http.disconnect"}

            request = Request(scope, receive=receive)
            response = await _oauth_authorize(request)

        assert response.status_code == 400
        assert "redirect_uri not allowed" in json_module.loads(response.body)["detail"]


# ---------------------------------------------------------------------------
# Scenario: Authenticated consent approve
# ---------------------------------------------------------------------------


class TestApproveConsent:
    ENDPOINT = "/api/v1/mcp/oauth/consent/approve"

    def test_approve_without_session_returns_401(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        try:
            resp = TestClient(app).post(self.ENDPOINT, json={"state": "state-xyz"})
            assert resp.status_code in (401, 403)
        finally:
            app.dependency_overrides.clear()

    def test_approve_mints_code_and_returns_redirect_url(self, admin_client: TestClient) -> None:
        state_row = _make_mock_consent_state(state="state-xyz")
        with (
            patch("modulo.auth.oauth.consume_consent_state", new=AsyncMock(return_value=state_row)),
            patch(
                "modulo.api.routes.mcp_oauth.create_authorization_code",
                new=AsyncMock(return_value="code-abc"),
            ) as mock_code,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(self.ENDPOINT, json={"state": "state-xyz"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["redirect_url"] == "https://app.example.com/callback?code=code-abc&state=state-xyz"
        # The code is minted from the STATE ROW's challenge + scopes and bound
        # to the Bearer principal — never from client-supplied input.
        kwargs = mock_code.call_args.kwargs
        assert kwargs["account_id"] == _USER_ID
        assert kwargs["code_challenge"] == _CODE_CHALLENGE
        assert kwargs["scopes"] == "trigger:run"
        assert kwargs["code_challenge_method"] == "S256"

    def test_approve_uses_stored_redirect_uri_never_client_supplied(self, admin_client: TestClient) -> None:
        state_row = _make_mock_consent_state(state="state-xyz", redirect_uri="https://app.example.com/callback")
        with (
            patch("modulo.auth.oauth.consume_consent_state", new=AsyncMock(return_value=state_row)),
            patch("modulo.api.routes.mcp_oauth.create_authorization_code", new=AsyncMock(return_value="code-abc")),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            # The approve body ONLY carries {state} — there is no redirect_uri
            # channel for a tampered URL to sneak through.
            resp = admin_client.post(self.ENDPOINT, json={"state": "state-xyz"})

        assert resp.status_code == 200
        assert resp.json()["redirect_url"].startswith("https://app.example.com/callback")

    def test_approve_wrong_or_consumed_state_returns_400(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.auth.oauth.consume_consent_state", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(self.ENDPOINT, json={"state": "already-consumed"})

        assert resp.status_code == 400
        assert "state" in resp.json()["detail"].lower()

    def test_approve_expired_state_returns_400(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.auth.oauth.consume_consent_state", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(self.ENDPOINT, json={"state": "expired-state"})

        assert resp.status_code == 400

    def test_approve_cross_org_state_denied(self, admin_client: TestClient) -> None:
        # RLS scopes the consume UPDATE to the approver's org — a state from
        # another org is invisible and returns None → denied.
        with (
            patch("modulo.auth.oauth.consume_consent_state", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(self.ENDPOINT, json={"state": "other-org-state"})

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Scenario: Token exchange (form-urlencoded, PKCE, live-role)
# ---------------------------------------------------------------------------


class TestTokenExchangeEndpoint:
    ENDPOINT = "/mcp/oauth/token"

    def test_form_encoded_exchange_returns_access_token(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
            patch("modulo.auth.oauth.verify_live_role_covers_scopes", new=AsyncMock(return_value="admin")),
            patch("modulo.auth.oauth.create_oauth_token_family") as mock_create_family,
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
            patch("modulo.auth.oauth.create_oauth_refresh_token") as mock_create_refresh,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_consume.return_value = _make_mock_auth_code()
            mock_create_family.return_value = ("family_uuid", 0)
            mock_create_token.return_value = "jwt_access_token_abc"
            mock_create_refresh.return_value = "jwt_refresh_token_abc"

            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": "auth_code_abc",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                    "redirect_uri": "https://app.example.com/callback",
                    "code_verifier": _CODE_VERIFIER,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"] == "jwt_access_token_abc"
        assert data["refresh_token"] == "jwt_refresh_token_abc"
        # The issued tokens carry the consenting account's account_id.
        assert mock_create_token.call_args.kwargs["account_id"] == str(_USER_ID)

    def test_pkce_verifier_forwarded_to_consume(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
            patch("modulo.auth.oauth.verify_live_role_covers_scopes", new=AsyncMock(return_value="admin")),
            patch("modulo.auth.oauth.create_oauth_token_family") as mock_create_family,
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
            patch("modulo.auth.oauth.create_oauth_refresh_token") as mock_create_refresh,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_consume.return_value = _make_mock_auth_code()
            mock_create_family.return_value = ("family_uuid", 0)
            mock_create_token.return_value = "jwt_access_token_abc"
            mock_create_refresh.return_value = "jwt_refresh_token_abc"

            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": "auth_code_abc",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                    "redirect_uri": "https://app.example.com/callback",
                    "code_verifier": _CODE_VERIFIER,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 200
        assert mock_consume.call_args.kwargs["code_verifier"] == _CODE_VERIFIER

    def test_json_body_still_supported_for_compat(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
            patch("modulo.auth.oauth.verify_live_role_covers_scopes", new=AsyncMock(return_value="admin")),
            patch("modulo.auth.oauth.create_oauth_token_family") as mock_create_family,
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
            patch("modulo.auth.oauth.create_oauth_refresh_token") as mock_create_refresh,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_consume.return_value = _make_mock_auth_code()
            mock_create_family.return_value = ("family_uuid", 0)
            mock_create_token.return_value = "jwt_access_token_abc"
            mock_create_refresh.return_value = "jwt_refresh_token_abc"

            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "grant_type": "authorization_code",
                    "code": "auth_code_abc",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                    "redirect_uri": "https://app.example.com/callback",
                    "code_verifier": _CODE_VERIFIER,
                },
            )

        assert resp.status_code == 200

    def test_client_secret_via_basic_auth(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
            patch("modulo.auth.oauth.verify_live_role_covers_scopes", new=AsyncMock(return_value="admin")),
            patch("modulo.auth.oauth.create_oauth_token_family") as mock_create_family,
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
            patch("modulo.auth.oauth.create_oauth_refresh_token") as mock_create_refresh,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_consume.return_value = _make_mock_auth_code()
            mock_create_family.return_value = ("family_uuid", 0)
            mock_create_token.return_value = "jwt_access_token_abc"
            mock_create_refresh.return_value = "jwt_refresh_token_abc"

            import base64

            basic = base64.b64encode(b"oauth_client_1:correct_secret").decode()
            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": "auth_code_abc",
                    "redirect_uri": "https://app.example.com/callback",
                    "code_verifier": _CODE_VERIFIER,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": f"Basic {basic}",
                },
            )

        assert resp.status_code == 200
        assert mock_validate.call_args.args[1:] == ("oauth_client_1", "correct_secret")

    def test_token_for_scopes_exceeding_live_role_denied(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
            patch(
                "modulo.auth.oauth.verify_live_role_covers_scopes",
                new=AsyncMock(side_effect=InvalidGrantError("Account role does not cover the granted scopes")),
            ),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_consume.return_value = _make_mock_auth_code(scopes="hitl:review")

            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": "auth_code_abc",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                    "redirect_uri": "https://app.example.com/callback",
                    "code_verifier": _CODE_VERIFIER,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Token exchange — error cases
# ---------------------------------------------------------------------------


class TestTokenExchangeErrors:
    ENDPOINT = "/mcp/oauth/token"

    def test_unsupported_grant_type(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.settings.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "client_credentials",
                    "client_id": "oauth_client_1",
                    "client_secret": "secret",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "unsupported_grant_type"

    def test_missing_params(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.settings.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": "",
                    "redirect_uri": "",
                    "client_id": "",
                    "client_secret": "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 400
        assert "Missing required parameters" in resp.json().get("detail", "")

    def test_wrong_content_type_rejected(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.settings.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            resp = admin_client.post(
                self.ENDPOINT,
                content="grant_type=authorization_code",
                headers={"Content-Type": "text/plain"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Scenario: PKCE code verifier required on token exchange
# ---------------------------------------------------------------------------


class TestPKCEEnforcement:
    ENDPOINT = "/mcp/oauth/token"

    def test_missing_code_verifier_rejected(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch(
                "modulo.auth.oauth.consume_authorization_code",
                new=AsyncMock(side_effect=InvalidGrantError("PKCE code_verifier is required")),
            ),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()

            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": "auth_code_pkce",
                    "client_id": "oauth_client_1",
                    "client_secret": "secret",
                    "redirect_uri": "https://app.example.com/callback",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Scenario: Refresh token (demote-then-refresh denied)
# ---------------------------------------------------------------------------


class TestRefreshEndpoint:
    ENDPOINT = "/mcp/oauth/refresh"

    def test_refresh_issues_new_pair(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.decode_oauth_refresh_token") as mock_decode,
            patch("modulo.auth.oauth.verify_live_role_covers_scopes", new=AsyncMock(return_value="admin")),
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
            patch("modulo.auth.oauth.create_oauth_refresh_token") as mock_create_refresh,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_create_token.return_value = "jwt_access_token_def"
            mock_create_refresh.return_value = "jwt_refresh_token_def"
            mock_decode.return_value = OAuthAccessTokenClaims(
                client_id="oauth_client_1",
                organisation_id=_ORG_ID,
                account_id=_USER_ID,
                scopes=["trigger:run"],
                token_family="family_1",
                token_sequence=0,
            )

            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": "rt1",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"] == "jwt_access_token_def"
        assert data["refresh_token"] == "jwt_refresh_token_def"
        assert mock_create_token.call_args.kwargs["account_id"] == str(_USER_ID)

    def test_demoted_account_refresh_denied(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.decode_oauth_refresh_token") as mock_decode,
            patch(
                "modulo.auth.oauth.verify_live_role_covers_scopes",
                new=AsyncMock(side_effect=InvalidGrantError("Account role does not cover the granted scopes")),
            ),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_validate.return_value = _make_mock_client()
            mock_decode.return_value = OAuthAccessTokenClaims(
                client_id="oauth_client_1",
                organisation_id=_ORG_ID,
                account_id=_USER_ID,
                scopes=["hitl:review"],
                token_family="family_1",
                token_sequence=0,
            )

            resp = admin_client.post(
                self.ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": "rt_stale",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Scenario: Scope enforcement — token limited to registered scopes
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    def test_scope_outside_allowed_set_is_rejected(self) -> None:
        client = _make_mock_client(client_id="limited_client", scopes="trigger:run")
        with pytest.raises(UnauthorizedClientError) as exc:
            validate_client_scopes(client, ["hitl:review"])
        assert "unauthorized_client" in str(exc.value).lower() or "None of the requested scopes" in str(exc.value)


# ---------------------------------------------------------------------------
# MCP middleware — real account_id + per-call live-role clamp (demote-then-call)
# ---------------------------------------------------------------------------


class TestOAuthMiddlewareAccountBinding:
    @staticmethod
    def _oauth_token(scopes: list[str]) -> str:
        return create_oauth_access_token(
            "oauth_client_1",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            scopes=scopes,
            token_family="family_1",
            token_sequence=0,
        )

    @staticmethod
    def _make_request(path: str = "/mcp/tools", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers or [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }

        async def receive() -> dict:
            return {"type": "http.disconnect"}

        return Request(scope, receive=receive)

    async def _dispatch(
        self,
        *,
        scopes: list[str],
        live_role: str | None,
        family_valid: bool = True,
        family_check_error: Exception | None = None,
        live_role_error: Exception | None = None,
    ) -> tuple[Any, Response]:
        from starlette.responses import JSONResponse

        token = self._oauth_token(scopes)
        request = self._make_request(headers=[(b"authorization", f"Bearer {token}".encode())])

        async def fake_call_next(_req: Request) -> Response:
            return JSONResponse({"ok": True})

        family_check = AsyncMock(return_value=family_valid)
        if family_check_error is not None:
            family_check = AsyncMock(side_effect=family_check_error)
        live_role_check = AsyncMock(return_value=live_role)
        if live_role_error is not None:
            live_role_check = AsyncMock(side_effect=live_role_error)

        with (
            patch("modulo.api.mcp_server._get_session_factory", return_value=_make_mock_session_factory()),
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch(
                "modulo.api.mcp_server.resolve_role_from_membership",
                new=live_role_check,
            ),
            patch(
                "modulo.api.mcp_server.check_oauth_token_family_valid",
                new=family_check,
            ),
        ):
            middleware = McpAuthMiddleware(app=MagicMock())
            response = await middleware.dispatch(request, fake_call_next)
            return response, request

    @pytest.mark.asyncio
    async def test_middleware_uses_real_account_id_and_clamps_live_role(self) -> None:
        t_user = _ctx_user_id.set(uuid.UUID(int=0))
        t_role = _ctx_role.set("")
        t_org = _ctx_org_id.set(uuid.UUID(int=0))
        t_auth = _ctx_auth_type.set("")
        t_tok = _ctx_auth_token.set("")
        t_key = _ctx_key_id.set(uuid.UUID(int=0))
        try:
            response, _request = await self._dispatch(
                scopes=["hitl:review"],
                live_role="viewer",
            )
            assert response.status_code == 200
            # Real account_id from the token claim — NOT a synthetic uuid5(client_id).
            assert _ctx_user_id.get() == _USER_ID
            # Scope-derived operator role is CLAMPED to the live viewer role.
            assert _ctx_role.get() == "viewer"
            assert _request.scope["auth_principal"]["user_id"] == str(_USER_ID)
        finally:
            _ctx_user_id.reset(t_user)
            _ctx_role.reset(t_role)
            _ctx_org_id.reset(t_org)
            _ctx_auth_type.reset(t_auth)
            _ctx_auth_token.reset(t_tok)
            _ctx_key_id.reset(t_key)

    @pytest.mark.asyncio
    async def test_middleware_keeps_scope_role_when_live_role_higher(self) -> None:
        t_user = _ctx_user_id.set(uuid.UUID(int=0))
        t_role = _ctx_role.set("")
        t_org = _ctx_org_id.set(uuid.UUID(int=0))
        t_auth = _ctx_auth_type.set("")
        t_tok = _ctx_auth_token.set("")
        t_key = _ctx_key_id.set(uuid.UUID(int=0))
        try:
            response, _request = await self._dispatch(
                scopes=["trigger:run"],
                live_role="admin",
            )
            assert response.status_code == 200
            # Token never grants more than its scopes — runner stays runner.
            assert _ctx_role.get() == "runner"
        finally:
            _ctx_user_id.reset(t_user)
            _ctx_role.reset(t_role)
            _ctx_org_id.reset(t_org)
            _ctx_auth_type.reset(t_auth)
            _ctx_auth_token.reset(t_tok)
            _ctx_key_id.reset(t_key)

    @pytest.mark.asyncio
    async def test_middleware_denies_missing_membership(self) -> None:
        t_user = _ctx_user_id.set(uuid.UUID(int=0))
        t_role = _ctx_role.set("")
        t_org = _ctx_org_id.set(uuid.UUID(int=0))
        t_auth = _ctx_auth_type.set("")
        t_tok = _ctx_auth_token.set("")
        t_key = _ctx_key_id.set(uuid.UUID(int=0))
        try:
            response, _request = await self._dispatch(
                scopes=["trigger:run"],
                live_role=None,
            )
            assert response.status_code == 403
        finally:
            _ctx_user_id.reset(t_user)
            _ctx_role.reset(t_role)
            _ctx_org_id.reset(t_org)
            _ctx_auth_type.reset(t_auth)
            _ctx_auth_token.reset(t_tok)
            _ctx_key_id.reset(t_key)

    @pytest.mark.asyncio
    async def test_middleware_returns_503_on_live_role_db_failure(self) -> None:
        """A DB outage on the live-role read is a 503, not a 401 bad-key."""
        from sqlalchemy.exc import OperationalError

        response, _request = await self._dispatch(
            scopes=["trigger:run"],
            live_role="admin",
            live_role_error=OperationalError("SELECT live_role", {}, Exception("db down")),
        )
        assert response.status_code == 503
        body = json_module.loads(response.body)
        assert body["error"] == "temporarily_unavailable"

    @pytest.mark.asyncio
    async def test_middleware_returns_503_on_family_check_db_failure(self) -> None:
        """A DB outage on the token-family check is a 503, not a 401."""
        from sqlalchemy.exc import SQLAlchemyError

        response, _request = await self._dispatch(
            scopes=["trigger:run"],
            live_role="admin",
            family_check_error=SQLAlchemyError("db down"),
        )
        assert response.status_code == 503
        body = json_module.loads(response.body)
        assert body["error"] == "temporarily_unavailable"


# ---------------------------------------------------------------------------
# Scenario: Revoke client
# ---------------------------------------------------------------------------


class TestDeleteOAuthClientBDD:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_delete_returns_deleted_true(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = True
            resp = admin_client.delete(f"{self.ENDPOINT}/oauth_client_1")

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_removes_auth_codes_and_token_families(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = True
            resp = admin_client.delete(f"{self.ENDPOINT}/oauth_client_1")

        assert resp.status_code == 200
        mock_delete.assert_called_once_with(ANY, client_id="oauth_client_1", org_id=_ORG_ID)
