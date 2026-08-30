"""Unit tests for the PUT /api/v1/me/password endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_STRONG_PW = "correct-horse-battery"
_NEW_PW = "new-strong-password-42"
_LOW_ENTROPY_PW = "11111111"
_SHORT_PW = "123"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_mock_user(password_hash: str | None = None) -> MagicMock:
    user = MagicMock()
    user.password_hash = password_hash
    return user


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestChangePassword:
    def test_password_change_needs_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(
            "/api/v1/me/password",
            json={
                "current_password": _STRONG_PW,
                "new_password": _NEW_PW,
            },
        )
        assert resp.status_code == 401

    def test_successful_password_change(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user) as mock_get,
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new_callable=AsyncMock,
            ),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["detail"] == "Password changed successfully"
        mock_get.assert_called_once()

    def test_successful_password_change_clears_must_change_flag(self, client: TestClient) -> None:
        """FAR-460: changing the password clears the admin-reset flag in the
        same transaction as the hash swap, satisfying the forced-change gate."""
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))
        user.must_change_password = True
        old_hash = user.password_hash

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user) as mock_get,
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new_callable=AsyncMock,
            ),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 200
        assert user.password_hash is not None
        assert user.password_hash != old_hash
        assert user.must_change_password is False
        mock_get.assert_called_once()

    def test_same_new_password_rejected(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _STRONG_PW,
                },
            )

        assert resp.status_code == 400
        assert "different" in resp.json()["detail"].lower()

    def test_wrong_current_password(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": "wrong-password",
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 400
        assert "incorrect" in resp.json()["detail"].lower()

    def test_low_entropy_new_password_rejected(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _LOW_ENTROPY_PW,
                },
            )

        assert resp.status_code == 422
        assert "entropy" in resp.json()["detail"].lower()

    def test_short_new_password_rejected_by_pydantic(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/me/password",
            json={
                "current_password": _STRONG_PW,
                "new_password": _SHORT_PW,
            },
        )

        assert resp.status_code == 422

    def test_no_local_password_returns_400(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=None)

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": "anything",
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 400
        assert "incorrect" in resp.json()["detail"].lower()

    def test_user_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=None),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 404

    def test_password_change_invalidates_token_families(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))
        mock_family_1 = MagicMock()
        mock_family_1.family_id = uuid.uuid4()
        mock_family_2 = MagicMock()
        mock_family_2.family_id = uuid.uuid4()

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user),
            patch(
                "modulo.api.routes.me.list_families_for_account",
                return_value=[mock_family_1, mock_family_2],
            ) as mock_list,
            patch("modulo.api.routes.me.blacklist_family", return_value=True) as mock_blacklist,
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new_callable=AsyncMock,
            ),
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 200
        mock_list.assert_called_once()
        assert mock_blacklist.call_count == 2

    def test_password_change_records_audit_event(self, client: TestClient) -> None:
        user = _make_mock_user(password_hash=hash_password(_STRONG_PW))

        with (
            patch("modulo.api.routes.me.get_account_by_id", return_value=user),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            resp = client.put(
                "/api/v1/me/password",
                json={
                    "current_password": _STRONG_PW,
                    "new_password": _NEW_PW,
                },
            )

        assert resp.status_code == 200
        mock_rls.assert_called_once()
        mock_audit.assert_called_once()
        _, kwargs = mock_audit.call_args
        assert kwargs["event_type"] == "password_changed"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "account"
        assert kwargs["resource_id"] == _USER_ID
