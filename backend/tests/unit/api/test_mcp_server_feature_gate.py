"""FAR-1283: server-side enforcement of the ``mcp_server`` plan feature.

The flag used to be honoured ONLY by the settings-page ``FeatureGate``, so an
organisation that turned it off hid the UI while every endpoint stayed live.
These tests pin the enforcement boundary on both halves of the MCP surface:

* the browser-authenticated OAuth client routes (``/api/v1/mcp/oauth/*``),
  gated with the project's ``require_feature("mcp_server")`` dependency, and
* the ``/mcp`` sub-app — authenticated requests gated in the auth middleware,
  and the three PRE-AUTH protocol endpoints (authorize/token/refresh) gated
  against the resolved client's org INSIDE the handler.

The pre-auth cases are the ones a mocked authenticated client cannot exercise,
so each is driven with NO credentials at all and asserts explicitly that the
response is not a 401 (a 401 there means the flag gate was wired to an
authenticated dependency chain and the OAuth flow is dead end-to-end — the
bug class this ticket's notes record for the SSO pre-auth routes).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import modulo.api.mcp_server as ms
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.mcp_server import (
    McpAuthMiddleware,
    _call_next_if_feature_enabled,
    _dispatch_unauth_paths,
    _mcp_feature_unavailable_response,
    _mcp_server_flag_enabled,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_coverage import audit_session
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FEATURE_TYPE = "urn:problem:modulo:feature_required"


@pytest.fixture(autouse=True)
def _stub_audit_session() -> Generator[None, None, None]:
    """FAR-1472: the fail-closed ``audited(...)`` dependency writes its event on a
    fresh ``audit_session`` (a real engine — no database in the unit tier), so
    stub that seam; the dependency itself still runs."""

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    app.dependency_overrides[audit_session] = _override
    yield
    app.dependency_overrides.pop(audit_session, None)


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


def _admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _client(feature_enabled: bool) -> Generator[TestClient, None, None]:
    """An authenticated admin TestClient with the flag forced on/off.

    ``get_plan_context`` is the single seam every ``require_feature`` gate
    resolves through, so overriding it exercises the real dependency (and the
    real 402 ProblemDetail) without a DB or a license.
    """
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _admin_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = feature_enabled
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    yield from _client(True)


@pytest.fixture
def flag_off_client() -> Generator[TestClient, None, None]:
    yield from _client(False)


_REGISTER_BODY = {
    "name": "My App",
    "redirect_uris": ["https://app.example.com/callback"],
    "scopes": ["trigger:run"],
}


# ---------------------------------------------------------------------------
# REST: the authenticated OAuth client routes
# ---------------------------------------------------------------------------


class TestOAuthClientRoutesFlagOff:
    """Every route denies with the structured feature-unavailable response."""

    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def _assert_feature_required(self, resp: object) -> None:
        assert resp.status_code == 402  # type: ignore[attr-defined]
        # Not a 401 (the caller IS authenticated) and not a 500.
        assert resp.status_code not in (401, 500)  # type: ignore[attr-defined]
        body = resp.json()  # type: ignore[attr-defined]
        assert body["type"] == _FEATURE_TYPE
        assert body["status"] == 402
        assert body["instance"] == "mcp_server"
        assert "mcp_server" in body["detail"]

    def test_register_denied(self, flag_off_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = flag_off_client.post(self.ENDPOINT, json=_REGISTER_BODY)
        # The gate runs before the handler: nothing is created.
        mock_create.assert_not_called()
        self._assert_feature_required(resp)

    def test_list_denied(self, flag_off_client: TestClient) -> None:
        with patch("modulo.api.routes.mcp_oauth.list_oauth_clients") as mock_list:
            resp = flag_off_client.get(self.ENDPOINT)
        mock_list.assert_not_called()
        self._assert_feature_required(resp)

    def test_delete_denied(self, flag_off_client: TestClient) -> None:
        with patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete:
            resp = flag_off_client.delete(f"{self.ENDPOINT}/myclient123")
        mock_delete.assert_not_called()
        self._assert_feature_required(resp)

    def test_consent_approve_denied(self, flag_off_client: TestClient) -> None:
        with patch("modulo.auth.oauth.consume_consent_state") as mock_consume:
            resp = flag_off_client.post(
                "/api/v1/mcp/oauth/consent/approve",
                json={"state": "abc"},
            )
        mock_consume.assert_not_called()
        self._assert_feature_required(resp)


class TestOAuthClientRoutesFlagOn:
    """With the flag on, behaviour is byte-identical to pre-FAR-1283."""

    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_register_returns_201(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_client = MagicMock()
            mock_client.id = uuid.uuid4()
            mock_client.client_id = "abc123def4567890"
            mock_client.name = "My App"
            mock_create.return_value = (mock_client, "raw_secret_40_chars_long_here")
            resp = admin_client.post(self.ENDPOINT, json=_REGISTER_BODY)

        assert resp.status_code == 201
        assert resp.json()["client_id"] == "abc123def4567890"

    def test_list_returns_200(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients") as mock_list,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_list.return_value = []
            resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert not resp.json()

    def test_delete_returns_200(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = True
            resp = admin_client.delete(f"{self.ENDPOINT}/myclient123")

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_consent_approve_returns_redirect(self, admin_client: TestClient) -> None:
        state_row = MagicMock()
        state_row.redirect_uri = "https://app.example.com/callback"
        state_row.client_id = "abc123def4567890"
        state_row.organisation_id = _ORG_ID
        state_row.scopes = ["trigger:run"]
        state_row.code_challenge = "challenge"
        with (
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            patch("modulo.auth.oauth.consume_consent_state", new=AsyncMock(return_value=state_row)),
            patch("modulo.auth.oauth.create_authorization_code", new=AsyncMock(return_value="code123")),
        ):
            resp = admin_client.post(
                "/api/v1/mcp/oauth/consent/approve",
                json={"state": "abc"},
            )

        assert resp.status_code == 200
        assert "code=" in resp.json()["redirect_url"]


def test_unauthenticated_still_401s_not_402() -> None:
    """No session + no flag decision available → 401, never a misleading 402."""
    app.dependency_overrides[get_settings] = _make_settings
    try:
        resp = TestClient(app).get("/api/v1/mcp/oauth/clients")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# /mcp sub-app: the authenticated surface (gate lives in the auth middleware)
# ---------------------------------------------------------------------------


def _mcp_probe_app() -> Starlette:
    """A minimal app carrying the REAL auth middleware + a sentinel route.

    The sentinel stands in for FastMCP so a test can prove the request was
    passed through (flag on) or never reached the app (flag off), without
    speaking the MCP protocol.
    """

    async def sentinel(request: object) -> PlainTextResponse:
        return PlainTextResponse("sentinel")

    return Starlette(
        routes=[Route("/", sentinel, methods=["GET", "POST"])],
        middleware=[Middleware(McpAuthMiddleware)],
    )


def _authed_request() -> MagicMock:
    request = MagicMock()
    request.headers = {"Authorization": "Bearer mk_testkey"}
    request.scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    return request


@pytest.mark.parametrize("token", ["mk_testkey", "oauth.jwt.token"])
def test_authenticated_mcp_request_denied_when_flag_off(token: str) -> None:
    """Every credential class is gated: 402 feature_required, app never runs."""
    calls: list[object] = []

    async def call_next(request: object) -> PlainTextResponse:
        calls.append(request)
        return PlainTextResponse("sentinel")

    async def _fake_api_key(request: MagicMock, tok: str) -> tuple[bool, None]:
        ms._ctx_org_id.set(_ORG_ID)
        return True, None

    async def _fake_oauth(request: MagicMock, tok: str, settings: object) -> tuple[bool, None, None]:
        ms._ctx_org_id.set(_ORG_ID)
        return True, None, None

    with (
        patch.object(ms, "_authenticate_api_key", _fake_api_key),
        patch.object(ms, "_authenticate_oauth_jwt", _fake_oauth),
        patch.object(ms, "_set_authz_enforce", new=AsyncMock()),
        patch.object(ms, "_mcp_server_feature_gate", new=AsyncMock(return_value=_mcp_feature_unavailable_response())),
        TestClient(_mcp_probe_app()) as c,
    ):
        response = c.post("/", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 402
    assert response.json() == {
        "error": "feature_required",
        "detail": "mcp_server is not available on your plan",
    }
    assert calls == []
    ms._ctx_org_id.set(None)


def test_authenticated_mcp_request_passes_when_flag_on() -> None:
    """Flag on → the request reaches the app unchanged."""

    async def _fake_api_key(request: MagicMock, tok: str) -> tuple[bool, None]:
        ms._ctx_org_id.set(_ORG_ID)
        return True, None

    with (
        patch.object(ms, "_authenticate_api_key", _fake_api_key),
        patch.object(ms, "_set_authz_enforce", new=AsyncMock()),
        patch.object(ms, "_mcp_server_feature_gate", new=AsyncMock(return_value=None)),
        TestClient(_mcp_probe_app()) as c,
    ):
        response = c.post("/", headers={"Authorization": "Bearer mk_testkey"})

    assert response.status_code == 200
    assert response.text == "sentinel"
    ms._ctx_org_id.set(None)


async def test_gate_is_fail_closed_on_flag_read_error() -> None:
    """A broken flag read denies — it must never widen access."""
    session = _make_mock_session()

    @asynccontextmanager
    async def _fake_session(org_id: uuid.UUID) -> AsyncGenerator[AsyncMock, None]:
        yield session

    with (
        patch.object(ms, "_session", _fake_session),
        patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        denied = await ms._mcp_server_feature_gate(_ORG_ID)

    assert denied is not None
    assert denied.status_code == 402


async def test_flag_read_cancellation_propagates() -> None:
    """A cancellation during the flag read must propagate, never be swallowed.

    ``CancelledError`` is a ``BaseException``; the explicit re-raise keeps a
    task cancellation aborting the request instead of being turned into a
    fail-closed 402. Exercises the ``except asyncio.CancelledError`` arm of
    ``_mcp_server_flag_enabled`` (the sibling of the generic-exception arm
    covered by ``test_gate_is_fail_closed_on_flag_read_error``).
    """
    session = _make_mock_session()

    with (
        patch(
            "modulo.db.crud.organisation.get_organisation",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _mcp_server_flag_enabled(_ORG_ID, session)


async def test_flag_enabled_reads_the_resolved_org_plan() -> None:
    """Resolution is org-scoped (org license/plan), not the system default."""
    session = _make_mock_session()
    org = MagicMock()
    plan_ctx = MagicMock()
    plan_ctx.feature_enabled.return_value = True

    with (
        patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(return_value=org)) as mock_get_org,
        patch.object(ms, "resolve_plan_context", new=AsyncMock(return_value=plan_ctx)) as mock_resolve,
        patch.object(ms, "get_settings", return_value=MagicMock()),
    ):
        assert await _mcp_server_flag_enabled(_ORG_ID, session) is True

    mock_get_org.assert_awaited_once_with(session, _ORG_ID)
    mock_resolve.assert_awaited_once()
    plan_ctx.feature_enabled.assert_called_with("mcp_server")


async def test_flag_disabled_returns_the_structured_response() -> None:
    session = _make_mock_session()
    plan_ctx = MagicMock()
    plan_ctx.feature_enabled.return_value = False

    with (
        patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(return_value=MagicMock())),
        patch.object(ms, "resolve_plan_context", new=AsyncMock(return_value=plan_ctx)),
        patch.object(ms, "get_settings", return_value=MagicMock()),
    ):
        assert await _mcp_server_flag_enabled(_ORG_ID, session) is False

    resp = _mcp_feature_unavailable_response()
    assert resp.status_code == 402


async def test_gate_without_org_context_is_not_an_enforcement_hole() -> None:
    """No org in context → nothing is gated, but nothing can read data either.

    Documents why passing through is safe: every tool handler resolves its
    tenant from the same ContextVars and fails closed without one.
    """
    ms._ctx_org_id.set(None)
    seen: list[object] = []

    async def call_next(request: object) -> PlainTextResponse:
        seen.append(request)
        return PlainTextResponse("sentinel")

    with patch.object(ms, "_mcp_server_flag_enabled", new=AsyncMock(return_value=False)) as mock_flag:
        response = await _call_next_if_feature_enabled(MagicMock(), call_next)

    mock_flag.assert_not_called()
    assert response.body == b"sentinel"
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# /mcp sub-app: the PRE-AUTH protocol endpoints (no credentials, no 401)
# ---------------------------------------------------------------------------

_AUTHORIZE_QUERY = {
    "response_type": "code",
    "client_id": "oauth_client_1",
    "redirect_uri": "https://app.example.com/callback",
    "scope": "trigger:run",
    "code_challenge": "challenge",
    "code_challenge_method": "S256",
    "state": "xyz",
}

_TOKEN_FORM = {
    "grant_type": "authorization_code",
    "code": "code123",
    "redirect_uri": "https://app.example.com/callback",
    "client_id": "oauth_client_1",
    "client_secret": "secret",
    "code_verifier": "verifier",
}

_REFRESH_FORM = {
    "grant_type": "refresh_token",
    "refresh_token": "refresh123",
    "client_id": "oauth_client_1",
    "client_secret": "secret",
}


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.organisation_id = _ORG_ID
    client.redirect_uris = "https://app.example.com/callback"
    return client


def _mock_factory() -> MagicMock:
    session = _make_mock_session()
    factory = MagicMock()
    factory.return_value = _session_context(session)
    return factory


def _session_context(session: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@contextmanager
def _pre_auth_client() -> Generator[TestClient, None, None]:
    """The REAL app, so the requests hit the public /mcp/oauth/* URLs.

    Only the mounted form dispatches unauthenticated (the sub-app is mounted at
    /mcp), so testing the sub-app in isolation would 401 every request and hide
    the very behaviour under test.
    """
    with (
        patch("modulo.core.rate_limiter.RateLimiterRegistry.check", new=AsyncMock(return_value=True)),
        patch("modulo.api.mcp_server.get_public_url", return_value="https://modulo.example.com"),
    ):
        yield TestClient(app)


async def test_protocol_endpoints_bypass_bearer_auth() -> None:
    """Invariant: all three OAuth legs stay reachable WITHOUT a session.

    ``_dispatch_unauth_paths`` is what keeps them pre-auth. If a future change
    routes them through the authenticated branch, they start 401-ing and the
    OAuth flow dies while every authenticated test still passes.
    """
    sentinel = object()
    paths = ("/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/refresh", "/mcp/healthz")

    async def call_next(request: object) -> object:
        return sentinel

    for path in paths:
        request = MagicMock()
        request.url = MagicMock(path=path)
        assert await _dispatch_unauth_paths(request, call_next) is sentinel
    other = MagicMock()
    other.url = MagicMock(path="/mcp/")
    assert await _dispatch_unauth_paths(other, call_next) is None


@pytest.mark.parametrize("path", ["/mcp/oauth/token", "/mcp/oauth/refresh"])
def test_pre_auth_grant_endpoints_are_denied_not_401(path: str) -> None:
    """Flag off, NO credentials: 402 feature_required — never 401, never 500."""
    form = _TOKEN_FORM if path.endswith("token") else _REFRESH_FORM
    with (
        patch("modulo.api.mcp_server._get_session_factory", return_value=_mock_factory()),
        patch("modulo.api.mcp_server.set_rls_org", new=AsyncMock()),
        patch("modulo.auth.oauth.validate_client_secret", new=AsyncMock(return_value=_mock_client())),
        patch("modulo.api.mcp_server._mcp_server_flag_enabled", new=AsyncMock(return_value=False)),
        _pre_auth_client() as c,
    ):
        resp = c.post(path, data=form)

    assert resp.status_code != 401, "pre-auth grant must never be gated on an authenticated dependency"
    assert resp.status_code == 402
    assert resp.json() == {
        "error": "feature_required",
        "detail": "mcp_server is not available on your plan",
    }


@pytest.mark.parametrize("path", ["/mcp/oauth/token", "/mcp/oauth/refresh"])
def test_pre_auth_grant_endpoints_reach_the_exchange_when_flag_on(path: str) -> None:
    """Flag on, NO credentials: the exchange is attempted (never a 401)."""
    form = _TOKEN_FORM if path.endswith("token") else _REFRESH_FORM
    with (
        patch("modulo.api.mcp_server._get_session_factory", return_value=_mock_factory()),
        patch("modulo.api.mcp_server.set_rls_org", new=AsyncMock()),
        patch("modulo.auth.oauth.validate_client_secret", new=AsyncMock(return_value=_mock_client())),
        patch("modulo.api.mcp_server._mcp_server_flag_enabled", new=AsyncMock(return_value=True)),
        patch("modulo.api.mcp_server._exchange_authorization_code", new=AsyncMock(return_value=None)) as mock_exchange,
        patch(
            "modulo.api.mcp_server._exchange_refresh_token", new=AsyncMock(return_value=None)
        ) as mock_exchange_refresh,
        _pre_auth_client() as c,
    ):
        resp = c.post(path, data=form)

    assert resp.status_code != 401
    # The handler got past the flag gate into the exchange seam and surfaced
    # its (mocked) failure — proof the pre-auth flow was not short-circuited.
    if path.endswith("token"):
        mock_exchange.assert_awaited_once()
    else:
        mock_exchange_refresh.assert_awaited_once()
    assert resp.status_code == 500


def test_pre_auth_authorize_is_denied_not_401_when_flag_off() -> None:
    """GET /mcp/oauth/authorize, no credentials, flag off → 402, never 401."""
    with (
        patch("modulo.api.mcp_server._get_session_factory", return_value=_mock_factory()),
        patch("modulo.api.mcp_server.set_rls_org", new=AsyncMock()),
        patch("modulo.auth.oauth.get_oauth_client_by_client_id", new=AsyncMock(return_value=_mock_client())),
        patch("modulo.api.mcp_server._mcp_server_flag_enabled", new=AsyncMock(return_value=False)),
        patch("modulo.auth.oauth.normalize_scopes", new=AsyncMock(return_value=["trigger:run"])),
        patch("modulo.auth.oauth.validate_client_scopes", new=AsyncMock(return_value=["trigger:run"])),
        _pre_auth_client() as c,
    ):
        resp = c.get("/mcp/oauth/authorize", params=_AUTHORIZE_QUERY)

    assert resp.status_code != 401, "authorize must never be gated on an authenticated dependency"
    assert resp.status_code == 402
    assert resp.json()["error"] == "feature_required"


def test_pre_auth_authorize_redirects_to_consent_when_flag_on() -> None:
    """Flag on, no credentials: the anonymous 302 to the SPA consent route."""
    with (
        patch("modulo.api.mcp_server._get_session_factory", return_value=_mock_factory()),
        patch("modulo.api.mcp_server.set_rls_org", new=AsyncMock()),
        patch("modulo.auth.oauth.get_oauth_client_by_client_id", new=AsyncMock(return_value=_mock_client())),
        patch("modulo.api.mcp_server._mcp_server_flag_enabled", new=AsyncMock(return_value=True)),
        patch("modulo.auth.oauth.normalize_scopes", new=AsyncMock(return_value=["trigger:run"])),
        patch("modulo.auth.oauth.validate_client_scopes", new=AsyncMock(return_value=["trigger:run"])),
        patch("modulo.auth.oauth.create_consent_state", new=AsyncMock()),
        _pre_auth_client() as c,
    ):
        resp = c.get(
            "/mcp/oauth/authorize",
            params=_AUTHORIZE_QUERY,
            follow_redirects=False,
        )

    assert resp.status_code != 401
    assert resp.status_code == 302
    assert "/oauth/authorize" in resp.headers["location"]


def test_pre_auth_authorize_writes_no_consent_state_when_flag_off() -> None:
    """The flag is checked BEFORE the consent row is written."""
    with (
        patch("modulo.api.mcp_server._get_session_factory", return_value=_mock_factory()),
        patch("modulo.api.mcp_server.set_rls_org", new=AsyncMock()),
        patch("modulo.auth.oauth.get_oauth_client_by_client_id", new=AsyncMock(return_value=_mock_client())),
        patch("modulo.api.mcp_server._mcp_server_flag_enabled", new=AsyncMock(return_value=False)),
        patch("modulo.auth.oauth.normalize_scopes", new=AsyncMock(return_value=["trigger:run"])),
        patch("modulo.auth.oauth.validate_client_scopes", new=AsyncMock(return_value=["trigger:run"])),
        patch("modulo.auth.oauth.create_consent_state", new=AsyncMock()) as mock_create_state,
        _pre_auth_client() as c,
    ):
        resp = c.get("/mcp/oauth/authorize", params=_AUTHORIZE_QUERY)

    assert resp.status_code == 402
    mock_create_state.assert_not_called()
