"""POST /api/v1/auth/refresh tests: cookie transport + account-active checks.

FAR-1197: the refresh token rides ONLY in the httpOnly ``modulo_refresh``
cookie — the JSON body no longer carries it. The endpoint is bodyless and gated
by a route-local double-submit CSRF check (see ``_require_csrf_double_submit``).

Deactivation must kill outstanding refresh families exactly like membership
removal does: every refresh re-reads ACCOUNT.ACTIVE, denies inactive/deleted
accounts, and blacklists the presented family inside the same transaction
(FAR-463).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.auth import REFRESH_COOKIE
from modulo.api.routes.auth import router as auth_router
from modulo.auth.jwt import create_refresh_token, decode_refresh_token_claims
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FAMILY_ID = "00000000-0000-0000-0000-00000000000f"
_CSRF_COOKIE = "XSRF-TOKEN"
_CSRF_VALUE = "unit-csrf-token"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        # Explicit: sibling unit/api conftests leak MODULO_CSRF_ENABLED=false into the
        # process env, and this module's CSRF pair tests must be env-independent.
        modulo_csrf_enabled=True,
    )


def _make_settings_with_refresh_ttl(ttl_hours: int) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_refresh_token_ttl_hours=ttl_hours,
        modulo_csrf_enabled=True,
    )


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _blacklist_update_sqls(session: AsyncMock) -> list[str]:
    """Return compiled SQL of any UPDATE token_families ... is_blacklisted statements."""
    sqls: list[str] = []
    for call in session.execute.call_args_list:
        stmt = call.args[0] if call.args else None
        if stmt is None:
            continue
        try:
            compiled = str(stmt.compile()).lower()
        except Exception:  # pragma: no cover - non-compilable debug object
            compiled = str(stmt).lower()
        if "update token_families" in compiled and "is_blacklisted" in compiled:
            sqls.append(compiled)
    return sqls


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    return _app


@pytest.fixture
def client(mock_session: AsyncMock, app: FastAPI) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _set_refresh_cookie(client: TestClient, token: str) -> None:
    """Arm the cookie jar with the refresh token and a matching CSRF pair.

    The XSRF cookie is reseeded before EVERY call to mirror the SPA: the
    double-submit header must always match the cookie the browser currently
    holds (a rotated response refreshes it in the jar, but callers may still
    be acting on a pre-rotation page state).
    """
    client.cookies.set(_CSRF_COOKIE, _CSRF_VALUE)
    for cookie in list(client.cookies.jar):
        if cookie.name == REFRESH_COOKIE:
            client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
    client.cookies.set(REFRESH_COOKIE, token)


def _post_refresh(client: TestClient, *, with_csrf_header: bool = True) -> "object":
    headers = {"X-CSRF-Token": _CSRF_VALUE} if with_csrf_header else None
    return client.post("/api/v1/auth/refresh", headers=headers)


def _make_refresh_token(org_id: str | None, sequence: int = 1) -> str:
    settings = _make_settings()
    return create_refresh_token(
        str(_ACCOUNT_ID),
        settings.secret_key,
        organisation_id=org_id or "",
        account_id=str(_ACCOUNT_ID),
        org_role="admin",
        token_family=_FAMILY_ID,
        token_sequence=sequence,
        client_kind="browser",
    )


def _make_account(active: bool) -> MagicMock:
    account = MagicMock()
    account.active = active
    account.email = "user@example.com"
    return account


def _patch_account(account: MagicMock | None):
    return patch("modulo.api.routes.auth.get_account_by_id", new=AsyncMock(return_value=account))


def test_refresh_success_for_active_account(client: TestClient, mock_session: AsyncMock) -> None:
    """An active account keeps refreshing normally; the family is untouched."""
    advance = AsyncMock(return_value=(2, False, False))
    resolve_role = AsyncMock(return_value="admin")
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=resolve_role),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(str(_ORG_ID)))
        resp = _post_refresh(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    # FAR-1197: the token never appears in a JSON body the SPA could persist.
    assert "refresh_token" not in body
    resolve_role.assert_awaited_once()
    advance.assert_awaited_once()
    # A successful refresh never touches the family blacklist.
    assert not _blacklist_update_sqls(mock_session)


def test_refresh_without_cookie_denied_fail_closed(client: TestClient) -> None:
    """No refresh cookie at all -> generic 401; no token-existence oracle."""
    client.cookies.set(_CSRF_COOKIE, _CSRF_VALUE)
    resp = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": _CSRF_VALUE})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or expired refresh token"


def test_refresh_without_csrf_pair_rejected_403(client: TestClient) -> None:
    """Missing/mismatched double-submit pair -> 403, fail closed (FAR-1197)."""
    client.cookies.set(REFRESH_COOKIE, _make_refresh_token(str(_ORG_ID)))
    missing = client.post("/api/v1/auth/refresh")
    assert missing.status_code == 403
    mismatched = client.post(
        "/api/v1/auth/refresh",
        headers={"X-CSRF-Token": _CSRF_VALUE},
    )
    assert mismatched.status_code == 403


def test_refresh_cookie_attributes(client: TestClient, mock_session: AsyncMock) -> None:
    """A rotating refresh response re-sets an httpOnly SameSite=strict Secure cookie."""
    advance = AsyncMock(return_value=(2, False, False))
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(str(_ORG_ID)))
        resp = _post_refresh(client)
    assert resp.status_code == 200, resp.text
    set_cookies = resp.headers.get_list("set-cookie")
    refresh_cookie = next(c for c in set_cookies if c.startswith(f"{REFRESH_COOKIE}="))
    assert "httponly" in refresh_cookie.lower()
    assert "samesite=strict" in refresh_cookie.lower()
    assert "secure" in refresh_cookie.lower()
    assert f"max-age={_make_settings().modulo_refresh_token_ttl_hours * 3600}" in refresh_cookie.lower()
    # The CSRF cookie is NOT httpOnly: the SPA must read it for the header.
    csrf_cookie = next(c for c in set_cookies if c.startswith("XSRF-TOKEN="))
    assert "httponly" not in csrf_cookie.lower()


def test_refresh_deactivated_account_denied_and_blacklisted(client: TestClient, mock_session: AsyncMock) -> None:
    """A deactivated account cannot refresh: 401, the presented family is
    blacklisted, and a subsequent attempt with the same family is denied too."""
    advance = AsyncMock(return_value=(2, False, False))
    token = _make_refresh_token(str(_ORG_ID))
    with (
        _patch_account(_make_account(False)),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, token)
        first = _post_refresh(client)
        _set_refresh_cookie(client, token)
        second = _post_refresh(client)
    assert first.status_code == 401
    assert second.status_code == 401
    assert first.json()["detail"] == "Account no longer has access to this organisation"
    # Sequence must NOT advance for a denied refresh.
    advance.assert_not_awaited()
    # Both denials attempted to persist the family blacklist (account-bound).
    blacklist_sqls = _blacklist_update_sqls(mock_session)
    assert len(blacklist_sqls) == 2
    for sql in blacklist_sqls:
        assert "is_blacklisted" in sql


def test_refresh_unknown_account_denied_and_blacklisted(client: TestClient, mock_session: AsyncMock) -> None:
    """A refresh token naming a non-existent account is denied and blacklisted."""
    advance = AsyncMock(return_value=(2, False, False))
    with (
        _patch_account(None),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(str(_ORG_ID)))
        resp = _post_refresh(client)
    assert resp.status_code == 401
    advance.assert_not_awaited()
    assert len(_blacklist_update_sqls(mock_session)) == 1


def test_refresh_deactivated_system_admin_without_membership_denied(
    client: TestClient, mock_session: AsyncMock
) -> None:
    """System admins without memberships (empty org_id) skip the membership read
    but the account-active check still denies them when deactivated."""
    advance = AsyncMock(return_value=(2, False, False))
    resolve_role = AsyncMock()
    with (
        _patch_account(_make_account(False)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=resolve_role),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(None))
        resp = _post_refresh(client)
    assert resp.status_code == 401
    resolve_role.assert_not_awaited()
    advance.assert_not_awaited()
    assert len(_blacklist_update_sqls(mock_session)) == 1


def test_refresh_active_system_admin_without_membership_succeeds(client: TestClient, mock_session: AsyncMock) -> None:
    """An ACTIVE system admin without memberships still refreshes: the new check
    gates on account status, not on org membership presence."""
    advance = AsyncMock(return_value=(2, False, False))
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock()),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(None))
        resp = _post_refresh(client)
    assert resp.status_code == 200, resp.text
    advance.assert_awaited_once()
    assert not _blacklist_update_sqls(mock_session)


def test_refresh_reuse_within_window_mints_tokens(client: TestClient, mock_session: AsyncMock) -> None:
    """A reuse within the grace window advances and mints (reuse_replay=True)."""
    advance = AsyncMock(return_value=(3, False, True))
    resolve_role = AsyncMock(return_value="admin")
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=resolve_role),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(str(_ORG_ID)))
        resp = _post_refresh(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert REFRESH_COOKIE in resp.cookies
    advance.assert_awaited_once()
    # Must NOT blacklist on a reuse replay
    assert not _blacklist_update_sqls(mock_session)


def test_refresh_rotation_mints_configured_lifetime(client: TestClient, app: FastAPI, mock_session: AsyncMock) -> None:
    """A non-default modulo_refresh_token_ttl_hours is stamped on the rotated
    refresh token — and on the httponly cookie's Max-Age (exp - iat == TTL)."""
    app.dependency_overrides[get_settings] = lambda: _make_settings_with_refresh_ttl(6)
    advance = AsyncMock(return_value=(2, False, False))
    resolve_role = AsyncMock(return_value="admin")
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=resolve_role),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        _set_refresh_cookie(client, _make_refresh_token(str(_ORG_ID)))
        resp = _post_refresh(client)
    assert resp.status_code == 200, resp.text
    rotated = resp.cookies[REFRESH_COOKIE]
    payload = decode_refresh_token_claims(rotated, _VALID_32)
    lifetime_seconds = float(payload["exp"]) - float(payload["iat"])
    assert abs(lifetime_seconds - 6 * 3600) <= 1
