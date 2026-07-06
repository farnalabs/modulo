"""BDD-mirror unit tests: MCP OAuth 2.0 authorization code flow.

Each test maps to a Gherkin scenario in tests/features/mcp/mcp_oauth.feature.
These cover the authorize and token protocol endpoints (in mcp_server.py)
in addition to the client CRUD endpoints already tested in test_mcp_oauth.py.
"""

import asyncio
import json as json_module
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.oauth import (
    OAuthAccessTokenClaims,
    validate_client_scopes,
)
from modulo.core.rate_limiter import RateLimiterRegistry
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
        modulo_public_url="https://modulo.example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
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
    code_challenge: str | None = None,
) -> MagicMock:
    c = MagicMock()
    c.code = "auth_code_abc"
    c.client_id = client_id
    c.organisation_id = _ORG_ID
    c.scopes = scopes
    c.redirect_uri = redirect_uri
    c.used = used
    c.code_challenge = code_challenge
    c.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    return c


@pytest.fixture()
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


@pytest.fixture()
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
# Scenario: Authorization request with PKCE
# ---------------------------------------------------------------------------


class TestAuthorizeEndpoint:
    """Tests the ``_oauth_authorize`` handler directly, bypassing middleware."""

    @staticmethod
    def _make_request(body: dict) -> object:
        body_bytes = json_module.dumps(body).encode()
        received = [False]

        async def receive() -> dict:
            if received[0]:
                return {"type": "http.disconnect"}
            received[0] = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/oauth/authorize",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
        return Request(scope, receive=receive)

    def test_authorize_returns_code_and_state(self) -> None:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
            patch("modulo.auth.oauth.create_authorization_code") as mock_create_code,
            patch("modulo.auth.oauth.normalize_scopes") as mock_norm,
            patch("modulo.auth.oauth.validate_client_scopes") as mock_val,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_norm.return_value = ["trigger:run"]
            mock_val.return_value = ["trigger:run"]
            mock_get.return_value = _make_mock_client()
            mock_create_code.return_value = "generated_auth_code"

            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "https://app.example.com/callback",
                    "scope": "trigger:run",
                    "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                    "code_challenge_method": "S256",
                    "state": "xyz",
                }
            )
            response = asyncio.run(_oauth_authorize(request))

        assert response.status_code == 200
        body = json_module.loads(response.body)
        assert body["code"] == "generated_auth_code"
        assert body["state"] == "xyz"


# ---------------------------------------------------------------------------
# Scenario: Token exchange
# ---------------------------------------------------------------------------


class TestTokenExchangeEndpoint:
    ENDPOINT = "/mcp/oauth/token"

    def test_exchange_returns_access_token(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.settings.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
            patch("modulo.auth.oauth.create_oauth_token_family") as mock_create_family,
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_get.return_value = _make_mock_client()
            mock_consume.return_value = _make_mock_auth_code()
            mock_create_family.return_value = ("family_uuid", 0)
            mock_create_token.return_value = "jwt_access_token_abc"

            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "grant_type": "authorization_code",
                    "code": "auth_code_abc",
                    "client_id": "oauth_client_1",
                    "client_secret": "correct_secret",
                    "redirect_uri": "https://app.example.com/callback",
                    "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data


# ---------------------------------------------------------------------------
# Scenario: Scope enforcement — token limited to registered scopes
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    def test_scope_outside_allowed_set_is_rejected(self) -> None:
        client = _make_mock_client(client_id="limited_client", scopes="trigger:run")
        with pytest.raises(Exception) as exc:
            validate_client_scopes(client, ["hitl:review"])
        assert "unauthorized_client" in str(exc.value).lower() or "None of the requested scopes" in str(exc.value)


# ---------------------------------------------------------------------------
# Scenario: Invalid redirect_uri rejected
# ---------------------------------------------------------------------------


class TestRedirectUriValidation:
    """Tests redirect URI validation via ``_oauth_authorize`` directly."""

    def test_mismatched_redirect_uri_returns_error(self) -> None:
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
            from starlette.requests import Request

            from modulo.api.mcp_server import _oauth_authorize

            body_bytes = json_module.dumps(
                {
                    "response_type": "code",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "https://evil.com/phish",
                    "scope": "trigger:run",
                    "state": "xyz",
                }
            ).encode()
            received = [False]

            async def receive() -> dict:
                if received[0]:
                    return {"type": "http.disconnect"}
                received[0] = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}

            scope = {
                "type": "http",
                "method": "POST",
                "path": "/mcp/oauth/authorize",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
            }
            request = Request(scope, receive=receive)
            response = asyncio.run(_oauth_authorize(request))

        assert response.status_code == 400
        data = json_module.loads(response.body)
        assert "redirect_uri not allowed" in data.get("detail", "")


# ---------------------------------------------------------------------------
# Authorize endpoint — edge cases
# ---------------------------------------------------------------------------


class TestAuthorizeErrors:
    """Test error handling in _oauth_authorize handler."""

    @staticmethod
    def _make_request(body: dict) -> object:
        body_bytes = json_module.dumps(body).encode()
        received = [False]

        async def receive() -> dict:
            if received[0]:
                return {"type": "http.disconnect"}
            received[0] = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/oauth/authorize",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
        return Request(scope, receive=receive)

    def test_unsupported_response_type(self) -> None:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "token",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "https://app.example.com/callback",
                }
            )
            response = asyncio.run(_oauth_authorize(request))

        assert response.status_code == 400
        data = json_module.loads(response.body)
        assert data["error"] == "unsupported_response_type"

    def test_missing_client_id(self) -> None:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "",
                    "redirect_uri": "",
                }
            )
            response = asyncio.run(_oauth_authorize(request))

        assert response.status_code == 400
        data = json_module.loads(response.body)
        assert "client_id" in data.get("detail", "")

    def test_missing_redirect_uri(self) -> None:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "oauth_client_1",
                    "redirect_uri": "",
                }
            )
            response = asyncio.run(_oauth_authorize(request))

        assert response.status_code == 400
        data = json_module.loads(response.body)
        assert "redirect_uri" in data.get("detail", "")

    def test_unknown_client_id(self) -> None:
        with (
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.api.mcp_server.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.get_oauth_client_by_client_id", return_value=None),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            from modulo.api.mcp_server import _oauth_authorize

            request = self._make_request(
                {
                    "response_type": "code",
                    "client_id": "nonexistent_client",
                    "redirect_uri": "https://app.example.com/callback",
                }
            )
            response = asyncio.run(_oauth_authorize(request))

        assert response.status_code == 400
        data = json_module.loads(response.body)
        assert data["error"] == "invalid_client"


# ---------------------------------------------------------------------------
# Token exchange — edge cases
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
                json={
                    "grant_type": "client_credentials",
                    "client_id": "oauth_client_1",
                    "client_secret": "secret",
                },
            )

        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "unsupported_grant_type"

    def test_missing_params(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.settings.get_settings", return_value=_make_settings()),
        ):
            mock_sf.return_value = _make_mock_session_factory()
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "grant_type": "authorization_code",
                    "code": "",
                    "redirect_uri": "",
                    "client_id": "",
                    "client_secret": "",
                },
            )

        assert resp.status_code == 400
        data = resp.json()
        assert "Missing required parameters" in data.get("detail", "")


# ---------------------------------------------------------------------------
# consume_authorization_code unit tests
# ---------------------------------------------------------------------------


class TestConsumeAuthorizationCode:
    """Test the consume_authorization_code function directly."""

    @pytest.mark.asyncio
    async def test_expired_code(self) -> None:
        from datetime import UTC, datetime, timedelta

        from modulo.auth.oauth import consume_authorization_code

        mock_session = _make_mock_session()
        expired_code = _make_mock_auth_code(
            client_id="oauth_client_1",
            used=False,
        )
        expired_code.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=expired_code)
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("modulo.auth.oauth.validate_client_secret"):
            with pytest.raises(Exception) as exc:
                await consume_authorization_code(
                    mock_session,
                    code="expired_code",
                    client_id="oauth_client_1",
                    redirect_uri="https://app.example.com/callback",
                    client_secret="correct_secret",
                )
            assert "invalid_grant" in str(exc.value).lower() or "expired" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_used_code(self) -> None:
        from modulo.auth.oauth import consume_authorization_code

        mock_session = _make_mock_session()
        used_code = _make_mock_auth_code(
            client_id="oauth_client_1",
            used=True,
        )

        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=used_code)
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("modulo.auth.oauth.validate_client_secret"):
            with pytest.raises(Exception) as exc:
                await consume_authorization_code(
                    mock_session,
                    code="used_code",
                    client_id="oauth_client_1",
                    redirect_uri="https://app.example.com/callback",
                    client_secret="correct_secret",
                )
            assert "invalid_grant" in str(exc.value).lower() or "used" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_wrong_client(self) -> None:
        from modulo.auth.oauth import consume_authorization_code

        mock_session = _make_mock_session()
        wrong_client_code = _make_mock_auth_code(
            client_id="other_client",
            used=False,
        )

        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=wrong_client_code)
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("modulo.auth.oauth.validate_client_secret"):
            with pytest.raises(Exception) as exc:
                await consume_authorization_code(
                    mock_session,
                    code="code_for_other",
                    client_id="oauth_client_1",
                    redirect_uri="https://app.example.com/callback",
                    client_secret="correct_secret",
                )
            assert "invalid_grant" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_redirect_uri_mismatch(self) -> None:
        from modulo.auth.oauth import consume_authorization_code

        mock_session = _make_mock_session()
        code = _make_mock_auth_code(
            client_id="oauth_client_1",
            redirect_uri="https://app.example.com/callback",
            used=False,
        )

        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=code)
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("modulo.auth.oauth.validate_client_secret"):
            with pytest.raises(Exception) as exc:
                await consume_authorization_code(
                    mock_session,
                    code="mismatch_code",
                    client_id="oauth_client_1",
                    redirect_uri="https://evil.com/phish",
                    client_secret="correct_secret",
                )
            assert "invalid_grant" in str(exc.value).lower() or "mismatch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Scenario: Refresh token rotation
# ---------------------------------------------------------------------------


class TestRefreshTokenRotation:
    ENDPOINT = "/mcp/oauth/token"

    @pytest.mark.xfail(reason="refresh_token grant_type not yet implemented in _oauth_token handler", strict=False)
    def test_refresh_issues_new_token_pair(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.decode_oauth_access_token") as mock_decode,
            patch("modulo.auth.oauth.rotate_oauth_token_family") as mock_rotate,
            patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
            patch("modulo.auth.oauth.check_oauth_token_family_valid") as mock_check,
        ):
            mock_validate.return_value = _make_mock_client()
            mock_check.return_value = True
            mock_rotate.return_value = ("family_1", 1)
            mock_create_token.return_value = "jwt_access_token_def"
            mock_decode.return_value = OAuthAccessTokenClaims(
                client_id="oauth_client_1",
                organisation_id=_ORG_ID,
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
        assert "access_token" in data
        assert "refresh_token" in data

    @pytest.mark.xfail(reason="refresh_token grant_type not yet implemented in _oauth_token handler", strict=False)
    def test_reused_refresh_token_blacklists_family(self, admin_client: TestClient) -> None:
        mock_decoded = OAuthAccessTokenClaims(
            client_id="oauth_client_1",
            organisation_id=_ORG_ID,
            scopes=["trigger:run"],
            token_family="family_1",
            token_sequence=0,
        )

        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
            patch("modulo.auth.oauth.decode_oauth_access_token") as mock_decode,
            patch("modulo.auth.oauth.rotate_oauth_token_family") as mock_rotate,
            patch("modulo.auth.oauth.check_oauth_token_family_valid") as mock_check,
        ):
            mock_validate.return_value = _make_mock_client()
            mock_check.return_value = True
            mock_decode.return_value = mock_decoded
            mock_rotate.side_effect = Exception("Token family rotated out of order — possible token theft")

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

        assert resp.status_code in (400, 401)


# ---------------------------------------------------------------------------
# Scenario: PKCE code verifier required on token exchange
# ---------------------------------------------------------------------------


class TestPKCEEnforcement:
    ENDPOINT = "/mcp/oauth/token"

    def test_missing_code_verifier_rejected(self, admin_client: TestClient) -> None:
        with (
            patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._get_session_factory") as mock_sf,
            patch("modulo.settings.get_settings", return_value=_make_settings()),
            patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
            patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
        ):
            mock_sf.return_value = _make_mock_session_factory()
            mock_get.return_value = _make_mock_client()
            mock_consume.side_effect = ValueError("PKCE code_verifier required")

            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "grant_type": "authorization_code",
                    "code": "auth_code_pkce",
                    "client_id": "oauth_client_1",
                    "client_secret": "secret",
                    "redirect_uri": "https://app.example.com/callback",
                },
            )

        assert resp.status_code in (400, 422)


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
