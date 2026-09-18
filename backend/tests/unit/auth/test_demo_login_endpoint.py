"""Demo auto-login endpoint tests (FAR-535) via FastAPI TestClient.

Locks the POST /api/v1/auth/demo contract: the kill switch answers a plain 404
when any of MODULO_DEMO_ENABLED / MODULO_DEMO_USER / MODULO_DEMO_PASSWORD is
unset or empty, a successful call mints an access token carrying the SHORT
demo TTL (modulo_demo_token_minutes, NOT modulo_access_token_minutes) with NO
refresh token and viewer-role demo-org claims, EVERY failure path — including
a credential mismatch between the env config and the stored account — answers
the same plain 404 (the endpoint takes no client credentials, so login-identical
401s would leak that the feature exists), an authenticating account WITHOUT a
viewer membership in the demo org (or with is_system_admin) is treated as
feature-absent (plain 404 — a demo request can never mint a token for another
org or an elevated role), the demo path NEVER touches the shared
AuthRateLimiter at ANY layer — handler AND middleware (anonymous demo visitors
cannot lock real users out of /login; the abuse cap is the per-IP
RateLimitMiddleware rule with its process-local token-bucket floor), the
auth.demo_login
audit event is emitted, the per-IP rate-limit rule (10/hour) is registered, and
the REAL _resolve_demo_org_membership (unmocked, against SQLite) only resolves
a live demo-org viewer membership.
"""

import logging
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware, RateLimitMiddleware
from modulo.api.routes.auth import _resolve_demo_org_membership
from modulo.api.routes.auth import router as auth_router
from modulo.auth.passwords import hash_password
from modulo.core.rate_limiter import AuthRateLimiter
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_DEMO_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_DEMO_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_DEMO_EMAIL = "demo@modulo.run"
_DEMO_PASSWORD = "demo-passphrase-123"
# Deliberately DIFFERENT from the demo TTL so the exp claim distinguishes which
# setting minted the token (normal login would produce 60-minute expiry here).
_NORMAL_TOKEN_MINUTES = 60
_DEMO_TOKEN_MINUTES = 30


def _settings(
    *,
    demo_enabled: bool = True,
    demo_user: str = _DEMO_EMAIL,
    demo_password: str = _DEMO_PASSWORD,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_access_token_minutes=_NORMAL_TOKEN_MINUTES,
        modulo_demo_enabled=demo_enabled,
        modulo_demo_user=demo_user,
        modulo_demo_password=demo_password,
        modulo_demo_token_minutes=_DEMO_TOKEN_MINUTES,
    )


def _demo_account(password_hash: str | None = None) -> MagicMock:
    account = MagicMock()
    account.id = _DEMO_USER_ID
    account.email = _DEMO_EMAIL
    account.display_name = "Demo"
    account.active = True
    account.is_system_admin = False
    account.must_change_password = False
    account.password_hash = password_hash if password_hash is not None else hash_password(_DEMO_PASSWORD)
    return account


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    return _app


def _override_settings(app: FastAPI, **kwargs: bool | str) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client(mock_session: AsyncMock, app: FastAPI) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    _override_settings(app)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("demo_enabled", "demo_user", "demo_password"),
    [
        pytest.param(False, _DEMO_EMAIL, _DEMO_PASSWORD, id="disabled"),
        pytest.param(True, "", _DEMO_PASSWORD, id="user-empty"),
        pytest.param(True, "   ", _DEMO_PASSWORD, id="user-whitespace"),
        pytest.param(True, _DEMO_EMAIL, "", id="password-empty"),
        pytest.param(False, "", "", id="all-unset"),
    ],
)
def test_demo_login_kill_switch_answers_plain_404(
    app: FastAPI, client: TestClient, demo_enabled: bool, demo_user: str, demo_password: str
) -> None:
    """Any missing/falsy piece of the demo env trio must 404 without DB access."""
    _override_settings(app, demo_enabled=demo_enabled, demo_user=demo_user, demo_password=demo_password)
    with patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=None)) as account_lookup:
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    account_lookup.assert_not_called()


def test_demo_login_success_mints_short_lived_demo_token(client: TestClient) -> None:
    account = _demo_account()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth._resolve_demo_org_membership",
            new=AsyncMock(return_value=(_DEMO_ORG_ID, "viewer")),
        ),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert "refresh_token" not in body
    claims = jwt.decode(body["access_token"], _VALID_32, algorithms=["HS256"])
    assert claims["sub"] == _DEMO_EMAIL
    assert claims["account_id"] == str(_DEMO_USER_ID)
    assert claims["org_id"] == str(_DEMO_ORG_ID)
    assert claims["org_role"] == "viewer"
    assert claims["is_system_admin"] is False
    # exp-iat must equal the SHORT demo TTL (30 min here), not the normal
    # modulo_access_token_minutes (60 min here) a /login token would carry.
    assert claims["exp"] - claims["iat"] == _DEMO_TOKEN_MINUTES * 60


def test_demo_login_sets_session_cookie_with_demo_ttl(client: TestClient) -> None:
    account = _demo_account()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth._resolve_demo_org_membership",
            new=AsyncMock(return_value=(_DEMO_ORG_ID, "viewer")),
        ),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 200
    assert "modulo_session" in resp.cookies
    session_cookie = next(
        cookie for cookie in resp.headers.get_list("set-cookie") if cookie.startswith("modulo_session=")
    )
    assert "Max-Age=1800" in session_cookie


def test_demo_login_unknown_env_user_answers_plain_404(app: FastAPI, client: TestClient) -> None:
    """Env user matching no real account yields the same plain 404 as everything else.

    The endpoint takes NO client credentials, so /login's 401 shape would serve
    no purpose here and would reveal that a demo feature exists.
    """
    _override_settings(app, demo_user="ghost@modulo.run")
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.auth.get_auth_rate_limiter", new=MagicMock(return_value=None)),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"
    assert "modulo_session" not in resp.cookies


def test_demo_login_env_password_mismatch_answers_plain_404(
    app: FastAPI, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A real demo account whose stored hash does not match the env password 404s.

    Stealth-consistent with every other demo failure (never a 401), and the
    mismatch is audit-logged with account context (no secrets) so operators
    can diagnose a bad MODULO_DEMO_PASSWORD.
    """
    _override_settings(app, demo_password="rotated-env-password")
    stale_account = _demo_account(password_hash=hash_password("old-stored-password"))
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=stale_account)),
        patch("modulo.api.routes.auth.get_auth_rate_limiter", new=MagicMock(return_value=None)),
        caplog.at_level(logging.WARNING, logger="modulo.api.routes.auth"),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"
    assert "modulo_session" not in resp.cookies
    mismatch_records = [
        record for record in caplog.records if record.getMessage() == "auth.demo_login_credential_mismatch"
    ]
    assert len(mismatch_records) == 1
    assert mismatch_records[0].account_id == str(_DEMO_USER_ID)
    assert mismatch_records[0].configured_user == _DEMO_EMAIL


def test_demo_login_never_touches_shared_auth_rate_limiter(app: FastAPI, client: TestClient) -> None:
    """The demo path must not interact with the shared AuthRateLimiter at all.

    Anonymous demo visitors must never be able to lock real users out of
    /login: no record_failure, no check_login, no record_success — the factory
    itself is never consulted, so no limiter state can move in either
    direction. The demo abuse cap is the per-IP RateLimitMiddleware rule.
    Handler-level guarantee; the middleware layer is locked separately by
    test_auth_middleware_demo_exempt_but_login_not.
    """
    _override_settings(app, demo_password="rotated-env-password")
    stale_account = _demo_account(password_hash=hash_password("old-stored-password"))
    limiter = AsyncMock(spec=AuthRateLimiter)
    limiter_factory = MagicMock(return_value=limiter)
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=stale_account)),
        patch("modulo.api.routes.auth.get_auth_rate_limiter", new=limiter_factory),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    limiter_factory.assert_not_called()
    limiter.record_failure.assert_not_called()
    limiter.record_success.assert_not_called()
    limiter.check_login.assert_not_called()


def test_auth_middleware_demo_exempt_but_login_not(mock_session: AsyncMock) -> None:
    """Middleware-level: POST /api/v1/auth/demo never invokes the auth limiter.

    Mounts AuthRateLimitMiddleware over the real auth router with a spied
    limiter: a POST to /api/v1/auth/demo must NOT call the shared limiter's
    check_login (the demo path is middleware-exempt — it can neither inherit
    /login lockouts nor re-arm lockout keys via setex), while a POST to
    /api/v1/auth/login still goes through it. Together with the handler-level
    test above this locks the invariant at EVERY layer.
    """
    limiter = AsyncMock(spec=AuthRateLimiter)
    limiter.check_login = AsyncMock(return_value=(True, 0))

    auth_app = FastAPI()
    auth_app.include_router(auth_router)
    auth_app.add_middleware(
        AuthRateLimitMiddleware,
        settings=_settings(),
        rate_limiter=limiter,
    )
    _override_settings(auth_app)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    auth_app.dependency_overrides[get_db_session] = override_session
    auth_app.dependency_overrides[_get_engine] = lambda: MagicMock()

    with (
        TestClient(auth_app) as mw_client,
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=None)),
    ):
        demo_resp = mw_client.post("/api/v1/auth/demo")
        assert demo_resp.status_code == 404
        # Assert BEFORE the /login request: check_login must still be untouched.
        limiter.check_login.assert_not_awaited()

        login_resp = mw_client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "pw"})
        assert login_resp.status_code == 401
        limiter.check_login.assert_awaited_once()


def test_demo_login_account_without_demo_org_membership_answers_plain_404(app: FastAPI, client: TestClient) -> None:
    """An authenticating account with NO viewer demo-org membership 404s.

    Defense against MODULO_DEMO_USER misconfiguration naming a pre-existing
    privileged account: its other-org memberships must never be mintable into
    a demo token — the endpoint resolves the session ONLY through the demo
    org slug + viewer role and treats everything else as feature-absent.
    """
    _override_settings(app, demo_user="existing-admin@example.com")
    existing_account = _demo_account()
    existing_account.email = "existing-admin@example.com"
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=existing_account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth._resolve_demo_org_membership",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"
    assert "modulo_session" not in resp.cookies


def test_demo_login_system_admin_account_answers_plain_404(app: FastAPI, client: TestClient) -> None:
    """is_system_admin=True on the authenticating account 404s without minting.

    Re-checked in the endpoint (not just the boot seed): an elevated account
    must never receive a demo session, and the membership resolver is not even
    consulted.
    """
    elevated_account = _demo_account()
    elevated_account.is_system_admin = True
    resolver = AsyncMock(return_value=(_DEMO_ORG_ID, "viewer"))
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=elevated_account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth._resolve_demo_org_membership", new=resolver),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"
    resolver.assert_not_called()
    assert "modulo_session" not in resp.cookies


def test_demo_login_success_logs_audit_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    account = _demo_account()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth._resolve_demo_org_membership",
            new=AsyncMock(return_value=(_DEMO_ORG_ID, "viewer")),
        ),
        caplog.at_level(logging.INFO, logger="modulo.api.routes.auth"),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 200
    demo_records = [record for record in caplog.records if record.getMessage() == "auth.demo_login"]
    assert demo_records
    assert demo_records[0].account_id == str(_DEMO_USER_ID)
    assert demo_records[0].org_id == str(_DEMO_ORG_ID)


def test_demo_rate_limit_rule_registered() -> None:
    """The RULES table must carry the 10/hour per-IP demo rule."""
    demo_rules = [rule for rule in RateLimitMiddleware.RULES if rule.path_prefix == "/api/v1/auth/demo"]
    assert len(demo_rules) == 1
    assert demo_rules[0].max_requests == 10
    assert demo_rules[0].window_s == 3600


# ---------------------------------------------------------------------------
# REAL _resolve_demo_org_membership (unmocked, in-memory SQLite)
# ---------------------------------------------------------------------------

# Tables the resolver touches (incl. accounts for the FK surface). Scoped
# create_all because unrelated models use Postgres-only column types SQLite
# cannot render.
_RESOLVER_TABLES = {"organisations", "accounts", "org_memberships"}


@pytest.fixture
async def resolver_session() -> AsyncGenerator[AsyncSession, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        wanted = [t for t in Base.metadata.sorted_tables if t.name in _RESOLVER_TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=wanted))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as session:
        yield session
    await eng.dispose()


async def _add_membership(
    session: AsyncSession,
    *,
    slug: str,
    role: str,
    deleted_at: datetime | None = None,
) -> Organisation:
    org = Organisation(name=slug.title(), slug=slug, settings_json={})
    if deleted_at is not None:
        org.deleted_at = deleted_at
    session.add(org)
    await session.flush()
    session.add(OrgMembership(account_id=_DEMO_USER_ID, organisation_id=org.id, role=role))
    await session.flush()
    return org


async def test_real_resolver_finds_viewer_membership_in_demo_org(resolver_session: AsyncSession) -> None:
    """A live demo-org viewer membership resolves to (org_id, 'viewer')."""
    org = await _add_membership(resolver_session, slug="demo", role="viewer")
    await resolver_session.commit()
    resolved = await _resolve_demo_org_membership(resolver_session, _DEMO_USER_ID)
    assert resolved == (org.id, "viewer")


async def test_real_resolver_ignores_other_org_membership(resolver_session: AsyncSession) -> None:
    """A membership in another org only never resolves — no token for another org."""
    await _add_membership(resolver_session, slug="other", role="viewer")
    await resolver_session.commit()
    resolved = await _resolve_demo_org_membership(resolver_session, _DEMO_USER_ID)
    assert resolved is None


async def test_real_resolver_ignores_non_viewer_role_in_demo_org(resolver_session: AsyncSession) -> None:
    """A demo-org membership with a non-viewer role never resolves — no elevated role."""
    await _add_membership(resolver_session, slug="demo", role="admin")
    await resolver_session.commit()
    resolved = await _resolve_demo_org_membership(resolver_session, _DEMO_USER_ID)
    assert resolved is None


async def test_real_resolver_ignores_soft_deleted_demo_org(resolver_session: AsyncSession) -> None:
    """A soft-deleted demo org answers None until the seed undeletes it."""
    await _add_membership(resolver_session, slug="demo", role="viewer", deleted_at=datetime.now(UTC))
    await resolver_session.commit()
    resolved = await _resolve_demo_org_membership(resolver_session, _DEMO_USER_ID)
    assert resolved is None
