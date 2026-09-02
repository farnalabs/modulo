"""Tests for self-lockout prevention via the shared last-admin guard.

The inline ``_prevent_last_admin_lockout`` check was REPLACED by the shared
``assert_not_last_admin`` guard (deliverable A of the break-glass plan), so
these tests exercise the new semantics: org-wide active/non-break-glass admin
counting, the Cannot-deactivate-yourself 422, and the break-glass-target 422.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Update

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.last_admin_guard import LastAdminLockoutError, assert_not_last_admin
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


# ── Direct unit tests of the shared guard (counting semantics) ────────


class _FakeResult:
    def __init__(self, *, first_value: tuple[object, object] | None = None, scalar_value: int | None = None) -> None:
        self._first = first_value
        self._scalar = scalar_value

    def first(self) -> tuple[object, object] | None:
        return self._first

    def scalar_one(self) -> int | None:
        return self._scalar


class _FakeBind:
    dialect = type("Dialect", (), {"name": "sqlite"})()


class _FakeSession:
    def __init__(self, *, target_active: bool = True, other_admins: int = 1) -> None:
        self.target_active = target_active
        self.other_admins = other_admins

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    async def execute(self, stmt: object, *args: object, **kwargs: object) -> _FakeResult:
        sql = str(stmt)
        if "count" in sql and "org_memberships" in sql:
            return _FakeResult(scalar_value=self.other_admins)
        return _FakeResult(first_value=(self.target_active, False))


class TestLastAdminGuardDirect:
    @pytest.mark.asyncio
    async def test_allows_promotion_to_admin(self) -> None:
        session = _FakeSession(other_admins=0)
        result = await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_USER_ID,
            target_role_after="admin",
            target_active_after=True,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_allows_when_target_is_not_last_admin(self) -> None:
        session = _FakeSession(other_admins=1)
        result = await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_ANOTHER_USER_ID,
            target_role_after="operator",
            target_active_after=False,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_role_unchanged_and_active(self) -> None:
        session = _FakeSession(other_admins=1)
        result = await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_USER_ID,
            target_role_after="admin",
            target_active_after=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_last_admin_self_demote(self) -> None:
        session = _FakeSession(other_admins=0, target_active=True)
        with pytest.raises(LastAdminLockoutError) as exc:
            await assert_not_last_admin(
                session,
                org_id=_ORG_ID,
                target_account_id=_USER_ID,
                target_role_after="operator",
                target_active_after=None,
            )
        assert "last admin" in exc.value.reason.lower()

    @pytest.mark.asyncio
    async def test_allows_self_demote_when_other_admin_exists(self) -> None:
        session = _FakeSession(other_admins=2)
        result = await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_USER_ID,
            target_role_after="runner",
            target_active_after=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_last_admin_self_demote_to_viewer(self) -> None:
        session = _FakeSession(other_admins=0, target_active=True)
        with pytest.raises(LastAdminLockoutError):
            await assert_not_last_admin(
                session,
                org_id=_ORG_ID,
                target_account_id=_USER_ID,
                target_role_after="viewer",
                target_active_after=None,
            )

    @pytest.mark.asyncio
    async def test_zero_admins_is_treated_as_single(self) -> None:
        session = _FakeSession(other_admins=0, target_active=True)
        with pytest.raises(LastAdminLockoutError):
            await assert_not_last_admin(
                session,
                org_id=_ORG_ID,
                target_account_id=_USER_ID,
                target_role_after="operator",
                target_active_after=None,
            )


# ── HTTP endpoint tests (wiring) ─────────────────────────────────


@pytest.fixture
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _patch_guard(**kwargs: object) -> patch:
    return patch("modulo.api.routes.admin.assert_not_last_admin", new_callable=AsyncMock, **kwargs)


class TestSelfLockoutEndpoint:
    URL = "/api/v1/admin/users/{user_id}"

    def test_self_demote_last_admin_returns_422(self, client: TestClient) -> None:
        """HTTP 422 when the guard rejects a last-admin self-demote."""
        with (
            _patch_guard(
                side_effect=LastAdminLockoutError(
                    org_id=_ORG_ID,
                    reason="Cannot remove the last admin. Promote another user to admin first.",
                )
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
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
        mock.is_break_glass = False
        mock.auth_provider = "local"
        mock.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        mock.last_login = None
        return mock

    def test_self_demote_with_another_admin_succeeds(self, client: TestClient) -> None:
        """HTTP 200 when the guard permits the self-demotion."""
        mock_account = self._make_mock_account(_USER_ID)

        mock_membership = MagicMock()
        mock_membership.role = "operator"
        mock_membership.deactivated_at = None

        with (
            _patch_guard(),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"org_role": "operator"},
            )
        assert resp.status_code == 200

    def test_self_deactivate_returns_422(self, client: TestClient) -> None:
        """Cannot-deactivate-yourself 422 is enforced on admin_update_user."""
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            _patch_guard(),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"is_active": False},
            )
        assert resp.status_code == 422
        assert "cannot deactivate yourself" in resp.json()["detail"].lower()

    def test_change_other_user_role_always_succeeds(self, client: TestClient) -> None:
        """Changing another user's role never triggers the guard."""
        mock_account = self._make_mock_account(_ANOTHER_USER_ID)

        mock_membership = MagicMock()
        mock_membership.role = "operator"
        mock_membership.deactivated_at = None

        with (
            _patch_guard(),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
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

        mock_membership = MagicMock()
        mock_membership.role = "admin"
        mock_membership.deactivated_at = None

        with (
            _patch_guard(),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"org_role": "admin"},
            )
        assert resp.status_code == 200

    def test_break_glass_target_returns_422(self, client: TestClient) -> None:
        """Break-glass accounts are 422-rejected on the admin update route."""
        mock_account = self._make_mock_account(_ANOTHER_USER_ID)
        mock_account.is_break_glass = True

        with (
            _patch_guard(),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
        ):
            resp = client.put(
                self.URL.format(user_id=_ANOTHER_USER_ID),
                json={"org_role": "operator"},
            )
        assert resp.status_code == 422
        assert "break-glass" in resp.json()["detail"].lower()

    def test_profile_only_update_of_sole_admin_returns_200(self, client: TestClient) -> None:
        """Profile-only updates (no org_role/is_active) must NOT 422 on the sole admin.

        Regression test for the false-positive 422: when a request changes only
        profile fields, req.org_role is None, and the last-admin guard must
        receive the target's CURRENT role rather than interpreting None as a
        demotion.
        """
        mock_account = self._make_mock_account(_USER_ID)
        mock_membership = MagicMock()
        mock_membership.role = "admin"

        guard_kwargs: dict[str, object] = {}

        async def _recording_guard(*_args: object, **kwargs: object) -> None:
            guard_kwargs.update(kwargs)

        with (
            patch(
                "modulo.api.routes.admin.assert_not_last_admin",
                new=_recording_guard,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"display_name": "Renamed User"},
            )
        assert resp.status_code == 200
        assert guard_kwargs.get("target_role_after") == "admin"
        assert guard_kwargs.get("target_active_after") is None

    def test_reactivate_via_put_clears_membership_tombstone(self, client: TestClient) -> None:
        """PUT /users/{id} with is_active=true must clear the deactivated_at tombstone.

        A user deactivated via the SECURITY DEFINER path is tombstoned on the
        membership (deactivated_at set). Reactivating via the admin update route
        must clear that tombstone so the user regains their org role. Per-org
        semantics (FAR-533): accounts.active is NOT touched — the account is
        not globally banned, so the mock reflects active=True.
        """
        mock_session = _make_mock_session()
        mock_account = self._make_mock_account(_USER_ID)
        mock_account.active = True
        mock_membership = MagicMock()
        mock_membership.role = "admin"
        mock_membership.deactivated_at = None

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

        with (
            _patch_guard(),
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_account_by_id", return_value=mock_account),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
        ):
            resp = client.put(
                self.URL.format(user_id=_USER_ID),
                json={"is_active": True},
            )
        app.dependency_overrides[get_db_session] = None

        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        update_statements = [
            call.args[0] for call in mock_session.execute.call_args_list if isinstance(call.args[0], Update)
        ]
        assert update_statements, "expected an UPDATE against org_memberships"
        assert any(
            "deactivated_at" in str(stmt) and stmt.compile().params.get("deactivated_at") is None
            for stmt in update_statements
        ), "reactivation must clear the membership deactivated_at tombstone"
