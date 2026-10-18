"""Regression tests: SSO callbacks must run DB work inside session.begin().

The DI session factory uses ``autobegin=False``. ``oidc_process_callback`` and
``saml_process_response`` execute SQL (JIT provisioning, group mappings, token
issuance) on the injected session, so the route handlers must wrap them in an
explicit transaction. These tests call the real handler against an autobegin-aware
fake session; the callback is mocked to perform a ``session.execute()`` (standing in
for the JIT-provisioning queries) so the pre-fix handler fails loudly.
"""

import urllib.parse
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request, Response
from sqlalchemy.exc import ProgrammingError

from modulo.api.exception_handlers import sso_error_redirect
from modulo.api.routes.sso import (
    _OIDC_LOGIN_STATE_COOKIE,
    _resolve_org_slug_for_error,
    _resolve_saml_for_route,
    oidc_callback,
    oidc_login,
)
from modulo.auth.sso import SsoCallbackError, SsoProvisioningDeniedError, sign_state, verify_state
from modulo.settings import Settings

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _AutobeginAwareSession:
    """Fake session whose execute() requires an explicit begin() first.

    ``scalar`` is what every read returns via ``scalar_one_or_none()`` — used
    by the FAR-1791 tests to make the org-slug lookup resolve a known slug.
    """

    def __init__(self, scalar: object = None) -> None:
        self._in_tx = False
        self._scalar = scalar

    def in_transaction(self) -> bool:
        return self._in_tx

    def begin(self) -> "_BeginCtx":
        return _BeginCtx(self)

    async def execute(self, stmt: object, *args: object) -> MagicMock:
        assert self._in_tx, "execute() ran outside session.begin() (autobegin=False)"
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._scalar
        return result


class _BeginCtx:
    """Async context manager returned by ``_AutobeginAwareSession.begin()``."""

    def __init__(self, session: _AutobeginAwareSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session._in_tx = True

    async def __aexit__(self, *_exc: object) -> bool:
        self._session._in_tx = False
        return False


def _make_callback_request(
    *, raw_state: str = "xyz", cookie_raw_state: str | None = "xyz", state: str | None = None
) -> Request:
    """Build a callback request carrying a validly-signed state.

    FAR-1979: the callback requires the returned ``state`` to match the
    login-time browser-binding cookie, so the default request carries BOTH a
    validly signed state and its matching cookie. Pass ``cookie_raw_state=None``
    for no cookie, a different value for a mismatched one, or ``state`` to
    override the query value verbatim, to exercise a rejected binding.
    """
    headers: list[tuple[bytes, bytes]] = []
    if cookie_raw_state is not None:
        cookie_value = sign_state(cookie_raw_state, _VALID_32)
        headers.append((b"cookie", f"{_OIDC_LOGIN_STATE_COOKIE}={cookie_value}".encode()))
    state_value = state if state is not None else sign_state(f"google:{raw_state}", _VALID_32)
    query_string = urllib.parse.urlencode({"code": "abc", "state": state_value}).encode()
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/oidc/google/callback",
            "query_string": query_string,
            "headers": headers,
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
            "root_path": "",
        }
    )


async def test_oidc_callback_runs_db_work_inside_begin() -> None:
    """The OIDC callback's DB work must run inside session.begin().

    The mocked ``oidc_process_callback`` performs a ``session.execute()`` (standing
    in for ``jit_provision_user``'s queries). The fake session only allows executes
    inside a transaction, so the pre-fix handler (bare call, no begin) fails while
    the fixed handler (``async with session.begin()``) redirects successfully.
    """
    settings = _make_settings()
    request = _make_callback_request()
    fake = _AutobeginAwareSession()

    async def fake_process_callback(
        code: str,
        state: str,
        s: Settings,
        system_session: object,
        app_session: object,
        redirect_uri: str,
    ) -> dict:
        assert isinstance(app_session, _AutobeginAwareSession)
        await app_session.execute(MagicMock())
        return {"access_token": "at", "refresh_token": "rt"}

    with patch("modulo.api.routes.sso.oidc_process_callback", new=fake_process_callback):
        resp = await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert resp.status_code == 307
    assert resp.headers["location"].endswith("#access_token=at")
    # FAR-1197: the refresh token is no longer sent in the fragment — it rides
    # in the httpOnly issuer cookie set on the redirect response.
    set_cookies = "; ".join(resp.headers.getlist("set-cookie"))
    assert "modulo_refresh" in set_cookies
    assert "XSRF-TOKEN" in set_cookies


async def test_saml_app_fallback_opens_scoped_transaction_when_caller_has_none() -> None:
    """FAR-1058: the SAML app fallback must open its own scoped transaction.

    The per-provider login/ACS routes hand the resolver an app session with no
    active transaction (``autobegin=False``). The pre-fix resolver bound RLS via
    ``_set_default_rls_org`` OUTSIDE any transaction, so every fallback hit
    raised ``RuntimeError`` -> HTTP 500 (a slug-enumeration oracle). The fake
    session only allows ``execute()`` inside ``begin()``, so the pre-fix path
    fails loudly while the fixed one binds inside its own scoped transaction.
    """
    fake = _AutobeginAwareSession()
    provider = SimpleNamespace(provider_type="saml", enabled=True)

    async def fake_set_default_rls_org(session: object) -> None:
        assert session is fake
        await fake.execute(MagicMock())

    async def fake_get_provider(session: object, provider_id: str) -> object:
        assert session is fake
        return provider

    with (
        patch(
            "modulo.api.routes.sso._read_system_saml_provider_by_slug",
            new=AsyncMock(return_value=None),
        ),
        patch("modulo.api.routes.sso._set_default_rls_org", new=fake_set_default_rls_org),
        patch("modulo.api.routes.sso.get_provider_by_provider_id", new=fake_get_provider),
    ):
        resolved = await _resolve_saml_for_route("okta-saml", None, fake)

    assert resolved is provider
    assert not fake.in_transaction()


# ---------------------------------------------------------------------------
# FAR-1791: SSO callback failures RAISE SsoCallbackError (rendered as a 303)
# ---------------------------------------------------------------------------
# The IdP navigates the BROWSER to the callback endpoints, so a failure raises
# SsoCallbackError — which the app's registered handler turns into a 303 back to
# the login page with a whitelisted code (never a raw JSON page, never exception
# text in the URL). The route RAISES rather than returning the redirect so the
# ``saml_acs_attempted`` audit dependency records an honest outcome="error".
# The real error stays logged.


def _make_request(query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/oidc/google/callback",
            "query_string": query_string,
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
            "root_path": "",
        }
    )


async def test_oidc_callback_join_denied_carries_org_for_org_login() -> None:
    """A FAR-855 denial carries the org, so the user lands on THAT org's login."""
    settings = _make_settings()
    request = _make_callback_request()
    org_id = uuid.uuid4()
    fake = _AutobeginAwareSession(scalar="acme")

    async def denying_callback(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise SsoProvisioningDeniedError("denied", org_id=org_id)

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=denying_callback),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "not_invited"
    assert exc_info.value.slug == "acme"


async def test_oidc_callback_join_denied_without_org_uses_base_login() -> None:
    """A denial with no resolvable org carries no slug (base /login target)."""
    settings = _make_settings()
    request = _make_callback_request()
    fake = _AutobeginAwareSession()

    async def denying_callback(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise SsoProvisioningDeniedError("denied")

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=denying_callback),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "not_invited"
    assert exc_info.value.slug is None


async def test_oidc_callback_value_error_raises_auth_failed() -> None:
    settings = _make_settings()
    request = _make_callback_request()
    fake = _AutobeginAwareSession()

    async def failing_callback(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise ValueError("Invalid state parameter — possible CSRF")

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=failing_callback),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "auth_failed"
    # The exception text must never reach the code (or the URL it builds).
    assert "CSRF" not in exc_info.value.code


async def test_oidc_callback_programming_error_raises_provider_unavailable() -> None:
    settings = _make_settings()
    request = _make_callback_request()
    fake = _AutobeginAwareSession()

    async def failing_callback(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise ProgrammingError("SELECT 1", {}, Exception("missing table"))

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=failing_callback),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "provider_unavailable"


async def test_oidc_callback_missing_code_or_state_raises_auth_failed() -> None:
    """The missing-code/state case raises rather than rendering JSON."""
    settings = _make_settings()
    request = _make_request()
    fake = _AutobeginAwareSession()

    with pytest.raises(SsoCallbackError) as exc_info:
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "auth_failed"


def test_sso_error_redirect_uses_303_and_a_whitelisted_url() -> None:
    resp = sso_error_redirect(_make_settings(), "auth_failed", None)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?sso_error=auth_failed")


def test_sso_error_redirect_quotes_the_slug() -> None:
    """A slug is server-resolved but must still be URL-quoted (defence in depth)."""
    resp = sso_error_redirect(_make_settings(), "not_invited", "acme corp")
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login/acme%20corp?sso_error=not_invited")


async def test_resolve_org_slug_for_error_none_short_circuits() -> None:
    assert await _resolve_org_slug_for_error(_AutobeginAwareSession(), None) is None


async def test_resolve_org_slug_for_error_read_failure_is_swallowed() -> None:
    """A slug-lookup fault must never mask the redirect on an already-failing path."""

    class _BoomSession:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> object:
            raise RuntimeError("session unusable")

    assert await _resolve_org_slug_for_error(_BoomSession(), uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# FAR-1979: the OIDC login state must be bound to the browser
# ---------------------------------------------------------------------------
# The signed ``state`` proves non-forgery but NOT that the callback belongs to
# the browser that began the flow — anyone can start a login at the IdP and
# obtain a validly signed state. Without a browser-carried correlation value an
# attacker can drive a victim's browser through a callback carrying the
# ATTACKER's code+state (login-CSRF into the attacker's account). These tests
# pin the fix: the login route sets an httpOnly correlation cookie and the
# callback REQUIRES it to match the returned state, failing closed.


def _cookie_header_value(response: Response) -> str | None:
    """Return the browser-binding Set-Cookie value, or None when it is absent."""
    for header in response.headers.getlist("set-cookie"):
        if header.startswith(f"{_OIDC_LOGIN_STATE_COOKIE}="):
            return header
    return None


async def test_oidc_login_sets_browser_binding_cookie() -> None:
    """The login redirect must set the short-lived httpOnly correlation cookie."""
    settings = _make_settings()
    fake = _AutobeginAwareSession()
    raw_state = str(uuid.uuid4())

    async def fake_authorize_url(*_args: object, **_kwargs: object) -> tuple[str, str]:
        return "https://idp.example.com/authorize?state=x", raw_state

    with patch("modulo.api.routes.sso.oidc_get_authorize_url", new=fake_authorize_url):
        resp = await oidc_login(
            provider="google",
            _request=_make_request(),
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert resp.status_code == 307
    cookie = _cookie_header_value(resp)
    assert cookie is not None
    assert "httponly" in cookie.lower()
    # LAX (not Strict) is required: the IdP -> callback leg is cross-site.
    assert "samesite=lax" in cookie.lower()
    cookie_value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert verify_state(cookie_value, settings.secret_key) == raw_state


async def test_oidc_callback_rejects_state_without_browser_cookie() -> None:
    """A validly signed state with NO matching cookie must be rejected (login-CSRF).

    Regression: pre-fix the callback accepted any validly signed state, so an
    attacker could replay their own state+code into a victim's browser. The
    binding check must run BEFORE the token exchange and fail closed.
    """
    settings = _make_settings()
    fake = _AutobeginAwareSession()
    request = _make_callback_request(cookie_raw_state=None)
    process = AsyncMock()

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=process),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "auth_failed"
    process.assert_not_awaited()


async def test_oidc_callback_rejects_cookie_bound_to_a_different_state() -> None:
    """A cookie from ANOTHER flow (different state) must not satisfy the binding."""
    settings = _make_settings()
    fake = _AutobeginAwareSession()
    request = _make_callback_request(raw_state="victim", cookie_raw_state="attacker")
    process = AsyncMock()

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=process),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "auth_failed"
    process.assert_not_awaited()


async def test_oidc_callback_rejects_tampered_state_even_with_a_cookie() -> None:
    """A state failing its HMAC check must be rejected regardless of the cookie."""
    settings = _make_settings()
    fake = _AutobeginAwareSession()
    request = _make_callback_request(state="google:xyz:deadbeef", cookie_raw_state="xyz")
    process = AsyncMock()

    with (
        patch("modulo.api.routes.sso.oidc_process_callback", new=process),
        pytest.raises(SsoCallbackError) as exc_info,
    ):
        await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert exc_info.value.code == "auth_failed"
    process.assert_not_awaited()


async def test_oidc_callback_clears_browser_binding_cookie_on_success() -> None:
    """A completed flow must expire the correlation cookie."""
    settings = _make_settings()
    fake = _AutobeginAwareSession()
    request = _make_callback_request()

    async def fake_process_callback(*_args: object, **_kwargs: object) -> dict[str, str]:
        await fake.execute(MagicMock())
        return {"access_token": "at", "refresh_token": "rt"}

    with patch("modulo.api.routes.sso.oidc_process_callback", new=fake_process_callback):
        resp = await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert resp.status_code == 307
    cookie = _cookie_header_value(resp)
    assert cookie is not None
    assert "max-age=0" in cookie.lower()
