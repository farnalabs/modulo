"""Tests for auth route ProgrammingError→501 handling.

Validates that all DB-accessing auth routes degrade gracefully
when the underlying table does not exist (migrations not yet run).
"""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _override() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_csrf_enabled=False,
    )


def _make_mock_account() -> MagicMock:
    account = MagicMock()
    account.id = _USER_ID
    account.email = "admin@example.com"
    account.display_name = "Admin User"
    account.active = True
    account.is_system_admin = False
    return account


def _mock_membership() -> MagicMock:
    m = MagicMock()
    m.organisation_id = _ORG_ID
    m.role = "admin"
    return m


def _mock_family() -> MagicMock:
    f = MagicMock()
    f.family_id = uuid.uuid4()
    return f


@pytest.fixture(autouse=True)
def _set_env() -> None:
    get_settings.cache_clear()


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = _override
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    yield TestClient(app)

    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Login — ProgrammingError on get_account_by_email → 501
# --------------------------------------------------------------------------


class TestLoginProgrammingError:
    def test_login_db_failure_returns_501(self, client: TestClient) -> None:
        exc = ProgrammingError("mock", "mock", "mock")
        mock_account = _make_mock_account()
        with (
            patch(
                "modulo.api.routes.auth.get_account_by_email",
                new=AsyncMock(return_value=mock_account),
            ),
            patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
            patch(
                "modulo.api.routes.auth.update_last_login",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "modulo.api.routes.auth.list_memberships_for_account",
                new=AsyncMock(return_value=[_mock_membership()]),
            ),
            patch(
                "modulo.api.routes.auth.create_family",
                new=AsyncMock(return_value=_mock_family()),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.com", "password": "testpass"},
            )
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_login_integrity_error_returns_409(self, client: TestClient) -> None:
        exc = IntegrityError("mock", "mock", "mock")
        mock_account = _make_mock_account()
        with (
            patch(
                "modulo.api.routes.auth.get_account_by_email",
                new=AsyncMock(return_value=mock_account),
            ),
            patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
            patch(
                "modulo.api.routes.auth.update_last_login",
                new=AsyncMock(),
            ),
            patch(
                "modulo.api.routes.auth.list_memberships_for_account",
                new=AsyncMock(return_value=[_mock_membership()]),
            ),
            patch(
                "modulo.api.routes.auth.create_family",
                new=AsyncMock(side_effect=exc),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.com", "password": "testpass"},
            )
        assert resp.status_code == 409
        assert "already has an active session" in resp.json()["detail"]

    def test_login_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        exc = SQLAlchemyError("mock", "mock", "mock")
        mock_account = _make_mock_account()
        with (
            patch(
                "modulo.api.routes.auth.get_account_by_email",
                new=AsyncMock(return_value=mock_account),
            ),
            patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
            patch(
                "modulo.api.routes.auth.update_last_login",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "modulo.api.routes.auth.list_memberships_for_account",
                new=AsyncMock(return_value=[_mock_membership()]),
            ),
            patch(
                "modulo.api.routes.auth.create_family",
                new=AsyncMock(return_value=_mock_family()),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.com", "password": "testpass"},
            )
        assert resp.status_code == 503


# --------------------------------------------------------------------------
# Refresh — ProgrammingError/SQLAlchemyError on advance_sequence → 501/503
# --------------------------------------------------------------------------


class TestRefreshProgrammingError:
    def test_refresh_db_failure_returns_501(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_refresh_token

        family_id = str(uuid.uuid4())
        refresh_token = create_refresh_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
            token_family=family_id,
            token_sequence=0,
        )

        exc = ProgrammingError("mock", "mock", "mock")
        with patch(
            "modulo.api.routes.auth.advance_sequence",
            new=AsyncMock(side_effect=exc),
        ):
            resp = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": refresh_token},
            )
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_refresh_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_refresh_token

        family_id = str(uuid.uuid4())
        refresh_token = create_refresh_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
            token_family=family_id,
            token_sequence=0,
        )

        exc = SQLAlchemyError("mock", "mock", "mock")
        with patch(
            "modulo.api.routes.auth.advance_sequence",
            new=AsyncMock(side_effect=exc),
        ):
            resp = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": refresh_token},
            )
        assert resp.status_code == 503


# --------------------------------------------------------------------------
# Logout — ProgrammingError/SQLAlchemyError on blacklist_family → 501/503
# --------------------------------------------------------------------------


class TestLogoutProgrammingError:
    def test_logout_db_failure_returns_501(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_refresh_token

        family_id = str(uuid.uuid4())
        refresh_token = create_refresh_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
            token_family=family_id,
            token_sequence=0,
        )

        exc = ProgrammingError("mock", "mock", "mock")
        with patch(
            "modulo.api.routes.auth.blacklist_family",
            new=AsyncMock(side_effect=exc),
        ):
            resp = client.post(
                "/api/v1/auth/logout",
                json={"refresh_token": refresh_token},
            )
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_logout_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_refresh_token

        family_id = str(uuid.uuid4())
        refresh_token = create_refresh_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
            token_family=family_id,
            token_sequence=0,
        )

        exc = SQLAlchemyError("mock", "mock", "mock")
        with patch(
            "modulo.api.routes.auth.blacklist_family",
            new=AsyncMock(side_effect=exc),
        ):
            resp = client.post(
                "/api/v1/auth/logout",
                json={"refresh_token": refresh_token},
            )
        assert resp.status_code == 503


# --------------------------------------------------------------------------
# Me — ProgrammingError/SQLAlchemyError on get_account_by_id → 501/503
# --------------------------------------------------------------------------


class TestMeProgrammingError:
    def test_me_db_failure_returns_501(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_access_token

        access_token = create_access_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
        )

        exc = ProgrammingError("mock", "mock", "mock")
        with patch(
            "modulo.api.routes.auth.get_account_by_id",
            new=AsyncMock(side_effect=exc),
        ):
            resp = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_me_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_access_token

        access_token = create_access_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
        )

        exc = SQLAlchemyError("mock", "mock", "mock")
        with patch(
            "modulo.api.routes.auth.get_account_by_id",
            new=AsyncMock(side_effect=exc),
        ):
            resp = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        assert resp.status_code == 503


# --------------------------------------------------------------------------
# Logout — blacklist_family returns False (already blacklisted or not found)
# --------------------------------------------------------------------------


class TestLogoutFamilyNotFound:
    def test_logout_idempotent_when_family_not_found(self, client: TestClient) -> None:
        from modulo.auth.jwt import create_refresh_token

        family_id = str(uuid.uuid4())
        refresh_token = create_refresh_token(
            "admin@example.com",
            _VALID_32,
            organisation_id=str(_ORG_ID),
            account_id=str(_USER_ID),
            org_role="admin",
            token_family=family_id,
            token_sequence=0,
        )

        with patch(
            "modulo.api.routes.auth.blacklist_family",
            new=AsyncMock(return_value=False),
        ):
            resp = client.post(
                "/api/v1/auth/logout",
                json={"refresh_token": refresh_token},
            )
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Logged out"
