"""Tests for self-lockout prevention guard (_prevent_last_admin_lockout)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.routes.admin import _prevent_last_admin_lockout
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ANOTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ── Direct unit tests of the guard function ──────────────────────


class TestPreventLastAdminLockoutDirect:
    """Direct unit tests of _prevent_last_admin_lockout."""

    @pytest.mark.asyncio
    async def test_allows_promotion_to_admin(self) -> None:
        """Setting role to 'admin' never triggers lockout."""
        session = AsyncMock()
        result = await _prevent_last_admin_lockout(
            current_account_id=_USER_ID,
            target_account_id=_USER_ID,
            org_id=_ORG_ID,
            new_role="admin",
            db_session=session,
        )
        assert result is None
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_target_is_not_self(self) -> None:
        """Changing another user's role does not trigger guard."""
        session = AsyncMock()
        result = await _prevent_last_admin_lockout(
            current_account_id=_USER_ID,
            target_account_id=_ANOTHER_USER_ID,
            org_id=_ORG_ID,
            new_role="operator",
            db_session=session,
        )
        assert result is None
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_role_unchanged(self) -> None:
        """No lockout risk when role is not being changed."""
        session = AsyncMock()
        result = await _prevent_last_admin_lockout(
            current_account_id=_USER_ID,
            target_account_id=_USER_ID,
            org_id=_ORG_ID,
            new_role=None,
            db_session=session,
        )
        assert result is None
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocks_last_admin_self_demote(self) -> None:
        """The last active admin cannot demote themselves."""
        session = AsyncMock()
        scalar_mock = MagicMock(return_value=1)
        result_mock = MagicMock()
        result_mock.scalar = scalar_mock
        session.execute.return_value = result_mock

        with pytest.raises(HTTPException) as exc:
            await _prevent_last_admin_lockout(
                current_account_id=_USER_ID,
                target_account_id=_USER_ID,
                org_id=_ORG_ID,
                new_role="operator",
                db_session=session,
            )
        assert exc.value.status_code == 422
        assert "last admin" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_allows_self_demote_when_other_admin_exists(self) -> None:
        """An admin can demote themselves if another active admin remains."""
        session = AsyncMock()
        scalar_mock = MagicMock(return_value=2)
        result_mock = MagicMock()
        result_mock.scalar = scalar_mock
        session.execute.return_value = result_mock

        result = await _prevent_last_admin_lockout(
            current_account_id=_USER_ID,
            target_account_id=_USER_ID,
            org_id=_ORG_ID,
            new_role="runner",
            db_session=session,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_last_admin_self_demote_to_viewer(self) -> None:
        """Last admin cannot demote to viewer either (or any non-admin role)."""
        session = AsyncMock()
        scalar_mock = MagicMock(return_value=1)
        result_mock = MagicMock()
        result_mock.scalar = scalar_mock
        session.execute.return_value = result_mock

        with pytest.raises(HTTPException) as exc:
            await _prevent_last_admin_lockout(
                current_account_id=_USER_ID,
                target_account_id=_USER_ID,
                org_id=_ORG_ID,
                new_role="viewer",
                db_session=session,
            )
        assert exc.value.status_code == 422
        assert "last admin" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_zero_admins_is_treated_as_single(self) -> None:
        """Zero admin count also triggers the guard (edge case / data integrity)."""
        session = AsyncMock()
        scalar_mock = MagicMock(return_value=0)
        result_mock = MagicMock()
        result_mock.scalar = scalar_mock
        session.execute.return_value = result_mock

        with pytest.raises(HTTPException) as exc:
            await _prevent_last_admin_lockout(
                current_account_id=_USER_ID,
                target_account_id=_USER_ID,
                org_id=_ORG_ID,
                new_role="operator",
                db_session=session,
            )
        assert exc.value.status_code == 422


# ── HTTP endpoint tests (wiring) ─────────────────────────────────


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSelfLockoutEndpoint:
    URL = "/api/v1/admin/users/{user_id}"

    def test_self_demote_last_admin_returns_422(self, client: TestClient) -> None:
        """HTTP 422 when last admin tries to self-demote."""
        scalar_mock = MagicMock(return_value=1)
        result_mock = MagicMock()
        result_mock.scalar = scalar_mock

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.execute.return_value = result_mock

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        with patch("modulo.api.routes.admin.set_rls_org"):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"org_role": "operator"},
            )
        assert resp.status_code == 422
        assert "last admin" in resp.json()["detail"].lower()

    def _make_mock_account(self, user_id: uuid.UUID, org_role: str = "admin") -> MagicMock:
        mock = MagicMock()
        mock.id = user_id
        mock.email = "admin@test.com"
        mock.display_name = "Admin User"
        mock.active = True
        mock.auth_provider = "local"
        mock.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        mock.last_login = None
        return mock

    def test_self_demote_with_another_admin_succeeds(self, client: TestClient) -> None:
        """HTTP 200 when self-demoting with another active admin."""
        mock_account = self._make_mock_account(_USER_ID)

        scalar_mock = MagicMock(return_value=2)
        result_mock = MagicMock()
        result_mock.scalar = scalar_mock

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.execute.return_value = result_mock
        mock_session.add = MagicMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        mock_membership = MagicMock()
        mock_membership.role = "operator"

        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"org_role": "operator"},
            )
        assert resp.status_code == 200

    def test_change_other_user_role_always_succeeds(self, client: TestClient) -> None:
        """Changing another user's role never triggers the guard."""
        mock_account = self._make_mock_account(_ANOTHER_USER_ID)

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        mock_membership = MagicMock()
        mock_membership.role = "operator"

        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_ANOTHER_USER_ID),
                json={"org_role": "operator"},
            )
        assert resp.status_code == 200

    def test_promote_to_admin_never_triggers_guard(self, client: TestClient) -> None:
        """Promoting a user to admin never triggers lockout (only demotion does)."""
        mock_account = self._make_mock_account(_USER_ID)

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        mock_membership = MagicMock()
        mock_membership.role = "admin"

        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"org_role": "admin"},
            )
        assert resp.status_code == 200
