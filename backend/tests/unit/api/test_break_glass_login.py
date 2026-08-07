"""Login-hook break-glass one-shot credential tests (deliverable B).

Covers the early-deny/late-consume decision in ``auth.login``:

* zero-query fast path for normal accounts (the login fast path is unchanged),
* fail-open for normal logins, fail-closed for break-glass accounts,
* CAS rowcount semantics (1 = consumed by this caller, 0 = already spent),
* ``record_failure`` on the hook-error / CAS-error 401,
* the late CAS as the FINAL DB statement (family-first, CAS-last),
* refresh denied once the credential is no longer live.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.auth import _consume_break_glass_credential, _enforce_break_glass
from modulo.api.routes.auth import router as auth_router
from modulo.auth.passwords import hash_password
from modulo.db.crud.token_family import consume_break_glass_credential
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FUTURE = datetime.now(UTC) + timedelta(hours=1)
_PAST = datetime.now(UTC) - timedelta(seconds=1)


class _FakeLimiter:
    """Minimal async rate limiter double recording failures/successes."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.successes: list[str] = []

    async def record_failure(self, ip: str) -> None:
        self.failures.append(ip)

    async def record_success(self, ip: str) -> None:
        self.successes.append(ip)


def _override(admin_password: str = "testpass") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password=admin_password,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
    )


def _make_user(
    *,
    is_break_glass: bool = False,
    expires_at: datetime | None = _FUTURE,
    deactivated_at: datetime | None = None,
    active: bool = True,
) -> MagicMock:
    user = MagicMock()
    user.id = _USER_ID
    user.email = "bg@example.com"
    user.display_name = "Break Glass"
    user.active = active
    user.password_hash = hash_password("testpass")
    user.is_system_admin = False
    user.is_break_glass = is_break_glass
    user.break_glass_expires_at = expires_at
    user.break_glass_deactivated_at = deactivated_at
    return user


def _make_membership() -> MagicMock:
    membership = MagicMock()
    membership.organisation_id = _ORG_ID
    membership.role = "admin"
    return membership


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
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock
    return session


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    return _app


@pytest.fixture
def client(mock_session: AsyncMock, app: FastAPI) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _override
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# _enforce_break_glass — early-deny/late-consume decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_break_glass_normal_account_zero_queries() -> None:
    """A normal account returns False immediately without consulting the deny
    predicate — the login fast path is unchanged (zero extra queries)."""
    user = _make_user(is_break_glass=False)
    limiter = _FakeLimiter()
    with patch("modulo.api.routes.auth.is_break_glass_denied", side_effect=AssertionError("must not be called")):
        decision = await _enforce_break_glass(user, now=_FUTURE, limiter=limiter, ip="1.2.3.4")
    assert decision is False
    assert limiter.failures == []


@pytest.mark.asyncio
async def test_enforce_break_glass_live_returns_true() -> None:
    now = datetime.now(UTC)
    user = _make_user(is_break_glass=True, expires_at=now + timedelta(hours=1))
    limiter = _FakeLimiter()
    decision = await _enforce_break_glass(user, now=now, limiter=limiter, ip="1.2.3.4")
    assert decision is True
    assert limiter.failures == []


@pytest.mark.parametrize(
    "expires_at,deactivated_at,active",
    [
        (_PAST, None, True),  # expired
        (None, None, True),  # NULL-expiry (CHECK-unrepresentable for live, deny defensively)
        (_FUTURE, _PAST, True),  # deactivated tombstone
        (_FUTURE, None, False),  # inactive
    ],
)
@pytest.mark.asyncio
async def test_enforce_break_glass_deny_raises_401_and_records_failure(
    expires_at: datetime | None, deactivated_at: datetime | None, active: bool
) -> None:
    user = _make_user(is_break_glass=True, expires_at=expires_at, deactivated_at=deactivated_at, active=active)
    limiter = _FakeLimiter()
    with pytest.raises(HTTPException) as exc_info:
        await _enforce_break_glass(user, now=_FUTURE, limiter=limiter, ip="1.2.3.4")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert limiter.failures == ["1.2.3.4"]


@pytest.mark.asyncio
async def test_enforce_break_glass_hook_error_fail_closed() -> None:
    """A hook error for a break-glass account is fail-closed: 401 + failure."""
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    limiter = _FakeLimiter()
    with (
        patch("modulo.api.routes.auth.is_break_glass_denied", side_effect=RuntimeError("boom")),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _enforce_break_glass(user, now=_FUTURE, limiter=limiter, ip="1.2.3.4")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert limiter.failures == ["1.2.3.4"]


@pytest.mark.asyncio
async def test_enforce_break_glass_no_limiter_skips_record_failure() -> None:
    user = _make_user(is_break_glass=True, expires_at=_PAST)
    with pytest.raises(HTTPException) as exc_info:
        await _enforce_break_glass(user, now=_FUTURE, limiter=None, ip="1.2.3.4")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# _consume_break_glass_credential — late CAS fail-closed semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_late_cas_rowcount_1_passes(mock_session: AsyncMock) -> None:
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    limiter = _FakeLimiter()
    with patch("modulo.api.routes.auth.consume_break_glass_credential", new=AsyncMock(return_value=1)) as cas:
        await _consume_break_glass_credential(mock_session, account=user, limiter=limiter, ip="1.2.3.4")
    cas.assert_awaited_once()
    assert cas.call_args.kwargs["account_id"] == _USER_ID
    assert cas.call_args.kwargs["current_password_hash"] == user.password_hash
    assert limiter.failures == []


@pytest.mark.asyncio
async def test_consume_late_cas_rowcount_0_raises_401(mock_session: AsyncMock) -> None:
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    limiter = _FakeLimiter()
    with (
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=AsyncMock(return_value=0)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _consume_break_glass_credential(mock_session, account=user, limiter=limiter, ip="1.2.3.4")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert limiter.failures == ["1.2.3.4"]


@pytest.mark.asyncio
async def test_consume_late_cas_error_fail_closed_401_not_503(mock_session: AsyncMock) -> None:
    """CAS ambiguity (a DB error) is fail-closed to 401 — never a 503 that
    could accidentally let a spent credential through."""
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    limiter = _FakeLimiter()
    with (
        patch(
            "modulo.api.routes.auth.consume_break_glass_credential",
            new=AsyncMock(side_effect=SQLAlchemyError("db down")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _consume_break_glass_credential(mock_session, account=user, limiter=limiter, ip="1.2.3.4")
    assert exc_info.value.status_code == 401
    assert limiter.failures == ["1.2.3.4"]


# ---------------------------------------------------------------------------
# consume_break_glass_credential — the CAS helper itself
# ---------------------------------------------------------------------------


def _cas_session(rowcount: int | None) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.rowcount = rowcount
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
async def test_cas_helper_rowcount_1() -> None:
    session = _cas_session(1)
    assert (
        await consume_break_glass_credential(
            session, account_id=_USER_ID, current_password_hash="old", new_password_hash="new"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cas_helper_rowcount_0() -> None:
    session = _cas_session(0)
    assert (
        await consume_break_glass_credential(
            session, account_id=_USER_ID, current_password_hash="old", new_password_hash="new"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_cas_helper_rowcount_none_is_zero() -> None:
    session = _cas_session(None)
    assert (
        await consume_break_glass_credential(
            session, account_id=_USER_ID, current_password_hash="old", new_password_hash="new"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_cas_helper_emits_raw_update_with_shared_live_predicate() -> None:
    """The CAS is a raw UPDATE against accounts whose WHERE is emitted from the
    shared builder, and it never touches updated_at (no TimestampMixin onupdate)."""
    session = _cas_session(1)
    await consume_break_glass_credential(
        session, account_id=_USER_ID, current_password_hash="old", new_password_hash="new"
    )
    stmt = session.execute.await_args.args[0]
    assert isinstance(stmt, TextClause)
    sql = str(stmt)
    assert "UPDATE public.accounts SET password_hash" in sql
    assert "accounts.id = :bg_account_id" in sql
    assert "accounts.password_hash = :bg_old_hash" in sql
    assert "accounts.is_break_glass IS true" in sql
    assert "accounts.break_glass_expires_at > current_timestamp" in sql
    assert "updated_at" not in sql


# ---------------------------------------------------------------------------
# Login route integration
# ---------------------------------------------------------------------------


def test_login_live_break_glass_consumes_and_200(client: TestClient) -> None:
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    family = MagicMock()
    family.family_id = uuid.uuid4()
    membership = _make_membership()
    cas = AsyncMock(return_value=1)
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[membership])),
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=cas),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": "bg@example.com", "password": "testpass"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()
    cas.assert_awaited_once()
    assert cas.call_args.kwargs["account_id"] == _USER_ID
    assert cas.call_args.kwargs["current_password_hash"] == user.password_hash


def test_login_consumed_break_glass_401_records_failure(client: TestClient) -> None:
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    limiter = _FakeLimiter()
    family = MagicMock()
    family.family_id = uuid.uuid4()
    membership = _make_membership()
    cas = AsyncMock(return_value=0)
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=limiter),
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[membership])),
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=cas),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": "bg@example.com", "password": "testpass"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"
    assert limiter.failures == ["testclient"]


def test_login_expired_break_glass_401_without_cas(client: TestClient) -> None:
    user = _make_user(is_break_glass=True, expires_at=_PAST)
    limiter = _FakeLimiter()
    cas = AsyncMock()
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=limiter),
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=cas),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": "bg@example.com", "password": "testpass"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"
    assert limiter.failures == ["testclient"]
    cas.assert_not_awaited()


def test_login_break_glass_cas_error_401_fail_closed(client: TestClient) -> None:
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    limiter = _FakeLimiter()
    family = MagicMock()
    family.family_id = uuid.uuid4()
    membership = _make_membership()
    cas = AsyncMock(side_effect=SQLAlchemyError("cas ambiguous"))
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=limiter),
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[membership])),
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=cas),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": "bg@example.com", "password": "testpass"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"
    assert limiter.failures == ["testclient"]


def test_login_normal_account_never_calls_cas(client: TestClient) -> None:
    user = _make_user(is_break_glass=False)
    family = MagicMock()
    family.family_id = uuid.uuid4()
    membership = _make_membership()
    cas = AsyncMock()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[membership])),
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=cas),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": "bg@example.com", "password": "testpass"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()
    cas.assert_not_awaited()


def test_login_wrong_password_break_glass_401(client: TestClient) -> None:
    """Wrong password on a break-glass account is the normal authenticate-fail
    401 — the hook never runs because authentication failed."""
    user = _make_user(is_break_glass=True, expires_at=_FUTURE)
    cas = AsyncMock()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=False),
        patch("modulo.api.routes.auth.consume_break_glass_credential", new=cas),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": "bg@example.com", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"
    cas.assert_not_awaited()


# ---------------------------------------------------------------------------
# Refresh denied (no write) for a no-longer-live break-glass account
# ---------------------------------------------------------------------------


def _refresh_token() -> str:
    from modulo.auth.jwt import create_refresh_token

    return create_refresh_token(
        subject="bg@example.com",
        secret_key=_VALID_32,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
        token_family=str(uuid.uuid4()),
        token_sequence=0,
    )


def test_refresh_denied_for_break_glass_no_write(client: TestClient) -> None:
    """ADR 017 live-role re-read denies refresh once the break-glass account is
    no longer live — advance_sequence (the only write in the refresh path) is
    never called, so no family mutation happens."""
    with (
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock()) as advance,
    ):
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": _refresh_token()})
    assert resp.status_code == 401
    advance.assert_not_awaited()
