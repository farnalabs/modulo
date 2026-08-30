"""POST /api/v1/auth/refresh tests: account-active defense-in-depth (FAR-463).

Deactivation must kill outstanding refresh families exactly like membership
removal does: every refresh re-reads ACCOUNT.ACTIVE, denies inactive/deleted
accounts, and blacklists the presented family inside the same transaction.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.auth import router as auth_router
from modulo.auth.jwt import create_refresh_token
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FAMILY_ID = "00000000-0000-0000-0000-00000000000f"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
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
    advance = AsyncMock(return_value=(2, False))
    resolve_role = AsyncMock(return_value="admin")
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=resolve_role),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": _make_refresh_token(str(_ORG_ID))})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    resolve_role.assert_awaited_once()
    advance.assert_awaited_once()
    # A successful refresh never touches the family blacklist.
    assert not _blacklist_update_sqls(mock_session)


def test_refresh_deactivated_account_denied_and_blacklisted(client: TestClient, mock_session: AsyncMock) -> None:
    """A deactivated account cannot refresh: 401, the presented family is
    blacklisted, and a subsequent attempt with the same family is denied too."""
    advance = AsyncMock(return_value=(2, False))
    token = _make_refresh_token(str(_ORG_ID))
    with (
        _patch_account(_make_account(False)),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        first = client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        second = client.post("/api/v1/auth/refresh", json={"refresh_token": token})
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
    advance = AsyncMock(return_value=(2, False))
    with (
        _patch_account(None),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": _make_refresh_token(str(_ORG_ID))})
    assert resp.status_code == 401
    advance.assert_not_awaited()
    assert len(_blacklist_update_sqls(mock_session)) == 1


def test_refresh_deactivated_system_admin_without_membership_denied(
    client: TestClient, mock_session: AsyncMock
) -> None:
    """System admins without memberships (empty org_id) skip the membership read
    but the account-active check still denies them when deactivated."""
    advance = AsyncMock(return_value=(2, False))
    resolve_role = AsyncMock()
    with (
        _patch_account(_make_account(False)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=resolve_role),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": _make_refresh_token(None)})
    assert resp.status_code == 401
    resolve_role.assert_not_awaited()
    advance.assert_not_awaited()
    assert len(_blacklist_update_sqls(mock_session)) == 1


def test_refresh_active_system_admin_without_membership_succeeds(client: TestClient, mock_session: AsyncMock) -> None:
    """An ACTIVE system admin without memberships still refreshes: the new check
    gates on account status, not on org membership presence."""
    advance = AsyncMock(return_value=(2, False))
    with (
        _patch_account(_make_account(True)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock()),
        patch("modulo.api.routes.auth.advance_sequence", new=advance),
    ):
        resp = client.post("/api/v1/auth/refresh", json={"refresh_token": _make_refresh_token(None)})
    assert resp.status_code == 200
    advance.assert_awaited_once()
    assert not _blacklist_update_sqls(mock_session)
