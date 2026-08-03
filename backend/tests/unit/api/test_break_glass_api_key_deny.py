"""Break-glass mint-deny behaviour (deliverable (B), API-key + long-lived deny).

A break-glass account — live or denied — hitting any secret-bearing
create/update/delete route gets a uniform 403 via the shared
``deny_break_glass_mint`` DI marker; a normal account is unaffected; the
fail-closed ``get_current_tenant_user_optional`` path denies break-glass
accounts on webhook routes. Read routes carry no marker and are unaffected.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_current_tenant_user_optional, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, create_access_token
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_KEY_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="http://localhost:8000",
    )


def _make_principal(is_break_glass: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="breakglass-user" if is_break_glass else "testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _make_account(*, is_break_glass: bool = True, live: bool = True) -> MagicMock:
    account = MagicMock()
    account.is_break_glass = is_break_glass
    account.active = True
    account.break_glass_deactivated_at = None
    account.break_glass_expires_at = (
        datetime.now(UTC) + timedelta(hours=1) if live else datetime.now(UTC) - timedelta(hours=1)
    )
    return account


def _make_session(account: object | None, *, raise_on_get: bool = False) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    if raise_on_get:
        session.get = AsyncMock(side_effect=SQLAlchemyError("db unavailable"))
    else:
        session.get = AsyncMock(return_value=account)
    return session


def _configure_auth(app_under_test: object, *, session: AsyncMock, principal: AuthenticatedPrincipal) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_user() -> AuthenticatedPrincipal:
        return principal

    app_under_test.dependency_overrides[get_db_session] = override_session
    app_under_test.dependency_overrides[get_current_user] = override_user


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_key() -> MagicMock:
    key = MagicMock()
    key.id = _KEY_ID
    key.name = "Test Key"
    key.role = "operator"
    key.lookup_prefix = "abcd1234"
    key.created_at = _NOW
    key.team_id = None
    return key


# ---------------------------------------------------------------------------
# 403 on secret-bearing mutations for a break-glass principal
# ---------------------------------------------------------------------------


def test_break_glass_cannot_create_api_key(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "operator"})
    assert resp.status_code == 403
    assert "Break-glass accounts" in resp.json()["detail"]


def test_break_glass_cannot_update_api_key(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.put(f"/api/v1/api-keys/{_KEY_ID}", json={"name": "renamed"})
    assert resp.status_code == 403


def test_break_glass_cannot_revoke_api_key(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.delete(f"/api/v1/api-keys/{_KEY_ID}")
    assert resp.status_code == 403


def test_break_glass_cannot_create_connector(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post(
        "/api/v1/connectors",
        json={"name": "c", "connector_type_id": "filesystem", "credentials": "sekret"},
    )
    assert resp.status_code == 403


def test_denied_break_glass_account_also_cannot_mint(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=False)),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "operator"})
    assert resp.status_code == 403


def test_mint_marker_fails_closed_on_db_read_error(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(None, raise_on_get=True),
        principal=_make_principal(is_break_glass=True),
    )
    resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "operator"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Normal accounts are unaffected
# ---------------------------------------------------------------------------


def test_normal_account_create_api_key_returns_201(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=False)),
        principal=_make_principal(is_break_glass=False),
    )
    key = _make_key()
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_test_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.api.routes.api_keys.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "Test Key", "role": "operator"})
    assert resp.status_code == 201


def test_normal_account_list_api_keys_is_unaffected_by_marker(client: TestClient) -> None:
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=False)),
        principal=_make_principal(is_break_glass=False),
    )
    with (
        patch("modulo.api.routes.api_keys.list_api_keys", return_value=[]),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/api-keys")
    assert resp.status_code == 200


def test_read_routes_are_not_mint_marked_for_break_glass(client: TestClient) -> None:
    """The marker covers create/update/delete only — reads are untouched."""
    _configure_auth(
        app,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
        principal=_make_principal(is_break_glass=True),
    )
    with (
        patch("modulo.api.routes.api_keys.list_api_keys", return_value=[]),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/api-keys")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fail-closed get_current_tenant_user_optional
# ---------------------------------------------------------------------------


def _optional_credentials(role: str = "admin") -> HTTPAuthorizationCredentials:
    settings = _make_settings()
    token = create_access_token(
        "breakglass-user",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role=role,
    )
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.asyncio
async def test_optional_path_denies_live_break_glass() -> None:
    settings = _make_settings()
    principal = await get_current_tenant_user_optional(
        credentials=_optional_credentials(),
        settings=settings,
        session=_make_session(_make_account(is_break_glass=True, live=True)),
    )
    assert principal is None


@pytest.mark.asyncio
async def test_optional_path_denies_denied_break_glass() -> None:
    settings = _make_settings()
    principal = await get_current_tenant_user_optional(
        credentials=_optional_credentials(),
        settings=settings,
        session=_make_session(_make_account(is_break_glass=True, live=False)),
    )
    assert principal is None


@pytest.mark.asyncio
async def test_optional_path_allows_normal_account() -> None:
    settings = _make_settings()
    principal = await get_current_tenant_user_optional(
        credentials=_optional_credentials(),
        settings=settings,
        session=_make_session(_make_account(is_break_glass=False)),
    )
    assert principal is not None
    assert principal.org_role == "admin"
    assert isinstance(principal, TenantPrincipal)


@pytest.mark.asyncio
async def test_optional_path_folds_to_none_when_account_unreadable() -> None:
    settings = _make_settings()
    principal = await get_current_tenant_user_optional(
        credentials=_optional_credentials(),
        settings=settings,
        session=_make_session(None, raise_on_get=True),
    )
    assert principal is None
