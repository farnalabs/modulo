"""Unit tests for /api/v1/admin endpoints (org deletion flow)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)
_TOKEN = "test-deletion-token-1234567890abcdef"
_TOKEN_EXPIRES = "2025-06-02T00:00:00+00:00"
_EXPORT = {
    "organisation": [
        {
            "id": str(_ORG_ID),
            "name": "Test Org",
            "slug": "test-org",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    ],
    "memberships": [{"id": str(_USER_ID), "email": "admin@test.com"}],
    "pipelines": [],
    "runs": [],
    "audit_events": [],
    "library_primitives": [],
    "connector_instances": [],
    "model_backends": [],
    "exported_at": "2025-06-01T12:00:00+00:00",
}
_OTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


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


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_rls_mock_session() -> AsyncMock:
    """Mock session usable by routes that call ``set_rls_org`` inside a transaction.

    ``set_rls_org``/``set_rls_user_context`` call ``session.in_transaction()`` and
    ``session.get_bind().dialect.name``; the plain ``_make_mock_session`` does not
    configure those, so the user offboarding routes (which run RLS setup inside
    ``async with session.begin()``) would raise RuntimeError.
    """
    session = _make_mock_session()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    return session


def _make_role_client(role: str, account_id: uuid.UUID) -> Generator[TestClient, None, None]:
    mock_session = _make_rls_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=account_id,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_rls_client() -> Generator[TestClient, None, None]:
    yield from _make_role_client("admin", _USER_ID)


@pytest.fixture
def operator_rls_client() -> Generator[TestClient, None, None]:
    yield from _make_role_client("operator", _USER_ID)


def _fake_offboarding_account(active: bool = False) -> MagicMock:
    account = MagicMock()
    account.id = _OTHER_USER_ID
    account.email = "user@test.com"
    account.display_name = "Test User"
    account.auth_provider = "local"
    account.created_at = _NOW
    account.last_login = None
    account.is_break_glass = False
    account.active = active
    return account


class TestUserDeactivateAuthorization:
    """Admin-only authorization for POST /admin/users/{id}/deactivate and /reactivate."""

    URL = "/api/v1/admin/users"

    def test_deactivate_non_admin_returns_403(self, operator_rls_client: TestClient) -> None:
        resp = operator_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")
        assert resp.status_code == 403

    def test_reactivate_non_admin_returns_403(self, operator_rls_client: TestClient) -> None:
        resp = operator_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/reactivate")
        assert resp.status_code == 403

    def test_self_deactivation_returns_422(self, admin_rls_client: TestClient) -> None:
        resp = admin_rls_client.post(f"{self.URL}/{_USER_ID}/deactivate")
        assert resp.status_code == 422
        assert "Cannot deactivate yourself" in resp.json()["detail"]

    def test_deactivate_unauthenticated_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")
        assert resp.status_code == 401

    def test_reactivate_unauthenticated_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"{self.URL}/{_OTHER_USER_ID}/reactivate")
        assert resp.status_code == 401

    def test_deactivate_malformed_uuid_returns_422(self, admin_rls_client: TestClient) -> None:
        resp = admin_rls_client.post(f"{self.URL}/not-a-uuid/deactivate")
        assert resp.status_code == 422

    def test_reactivate_malformed_uuid_returns_422(self, admin_rls_client: TestClient) -> None:
        resp = admin_rls_client.post(f"{self.URL}/not-a-uuid/reactivate")
        assert resp.status_code == 422

    def test_deactivate_removes_all_team_memberships(self, admin_rls_client: TestClient) -> None:
        membership_one = MagicMock()
        membership_one.id = uuid.uuid4()
        membership_two = MagicMock()
        membership_two.id = uuid.uuid4()
        fake_membership = MagicMock()
        fake_membership.role = "admin"

        with (
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account()),
            ),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=fake_membership),
            ),
            patch("modulo.api.routes.admin.assert_not_last_admin", AsyncMock()),
            patch(
                "modulo.api.routes.admin.list_team_memberships_for_account",
                AsyncMock(return_value=[membership_one, membership_two]),
            ),
            patch(
                "modulo.api.routes.admin.remove_team_member",
                AsyncMock(return_value=True),
            ) as remove_member,
            patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
        ):
            resp = admin_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")

        assert resp.status_code == 200
        assert remove_member.await_count == 2
        remove_member.assert_any_await(ANY, membership_one.id)
        remove_member.assert_any_await(ANY, membership_two.id)

    def test_deactivate_success_returns_inactive_user(self, admin_rls_client: TestClient) -> None:
        fake_membership = MagicMock()
        fake_membership.role = "admin"

        with (
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account(active=False)),
            ),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=fake_membership),
            ),
            patch("modulo.api.routes.admin.assert_not_last_admin", AsyncMock()),
            patch("modulo.api.routes.admin.list_team_memberships_for_account", AsyncMock(return_value=[])),
            patch("modulo.api.routes.admin.remove_team_member", AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
        ):
            resp = admin_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/deactivate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"] is False
        assert body["id"] == str(_OTHER_USER_ID)

    def test_reactivate_success_returns_active_user(self, admin_rls_client: TestClient) -> None:
        fake_membership = MagicMock()
        fake_membership.role = "admin"

        with (
            patch(
                "modulo.api.routes.admin.get_account_by_id",
                AsyncMock(return_value=_fake_offboarding_account(active=True)),
            ),
            patch(
                "modulo.api.routes.admin.get_membership_by_account_and_org",
                AsyncMock(return_value=fake_membership),
            ),
            patch("modulo.core.audit_logger.append_audit_event", AsyncMock()),
        ):
            resp = admin_rls_client.post(f"{self.URL}/{_OTHER_USER_ID}/reactivate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"] is True


class TestDeletionRequest:
    URL = "/api/v1/admin/org/deletion-request"

    def test_admin_requests_deletion_returns_202(self, client: TestClient) -> None:
        crud_result = {
            "token": _TOKEN,
            "token_expires_at": _TOKEN_EXPIRES,
            "export": _EXPORT,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                return_value=crud_result,
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 202
        data = resp.json()
        assert data["token"] == _TOKEN
        assert data["token_expires_at"] == _TOKEN_EXPIRES
        assert data["export_summary"]["organisation"] == "Test Org"
        assert data["export_summary"]["user_count"] == 1

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL)
        assert resp.status_code in (401, 403)

    def test_already_deleted_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation is already deleted"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 409

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 409


class TestDeletionConfirm:
    URL = "/api/v1/admin/org/deletion-confirm"

    def test_admin_confirms_deletion_returns_200(self, client: TestClient) -> None:
        crud_result = {
            "deleted_organisation_id": str(_ORG_ID),
            "hard_deleted_runs": 5,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                return_value=crud_result,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 5
        assert "permanently deleted" in data["message"]

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code in (401, 403)

    def test_invalid_token_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Invalid deletion token"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": "wrong"})
        assert resp.status_code == 409

    def test_expired_token_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Deletion token has expired"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 409

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"token": _TOKEN})
        assert resp.status_code == 409


class TestOrgExport:
    URL = "/api/v1/admin/org/export"

    def test_admin_exports_org_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.export_org_data",
                return_value=_EXPORT,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["organisation"]["name"] == "Test Org"
        assert data["organisation"]["status"] == "active"
        assert data["exported_at"] == "2025-06-01T12:00:00+00:00"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_org_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.export_org_data",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 404


class TestDeleteOrgImmediate:
    URL = "/api/v1/admin/org"

    def test_admin_deletes_org_returns_200(self, client: TestClient) -> None:
        request_result = {
            "token": _TOKEN,
            "token_expires_at": _TOKEN_EXPIRES,
            "export": _EXPORT,
        }
        confirm_result = {
            "deleted_organisation_id": str(_ORG_ID),
            "hard_deleted_runs": 0,
        }
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                return_value=request_result,
            ),
            patch(
                "modulo.db.crud.org_deletion.confirm_org_deletion",
                return_value=confirm_result,
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_organisation_id"] == str(_ORG_ID)
        assert data["hard_deleted_runs"] == 0
        assert "permanently deleted" in data["message"]

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.delete(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete(self.URL)
        assert resp.status_code in (401, 403)

    def test_org_not_found_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation not found"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 409

    def test_already_deleted_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.org_deletion.request_org_deletion",
                side_effect=ValueError("Organisation is already deleted"),
            ),
            patch("modulo.core.audit_logger.append_audit_event"),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(self.URL)
        assert resp.status_code == 409


class TestAdminListTeamsOwnedResourceCount:
    """GET /api/v1/admin/teams includes owned_resource_count (PRD §9.3)."""

    URL = "/api/v1/admin/teams"

    def _mock_team(self, team_id: uuid.UUID, name: str) -> MagicMock:
        t = MagicMock()
        t.id = team_id
        t.organisation_id = _ORG_ID
        t.name = name
        t.description = None
        t.account_id = _USER_ID
        t.created_at = _NOW
        t.updated_at = _NOW
        return t

    def test_includes_owned_resource_count(self, admin_rls_client: TestClient) -> None:
        team_a = self._mock_team(uuid.uuid4(), "Team A")
        team_b = self._mock_team(uuid.uuid4(), "Team B")
        page_result = MagicMock(items=[team_a, team_b], total=2, page=1, page_size=20)

        # Configure the RLS mock session so the member-count GROUP BY query returns [team_a:3].
        from modulo.api.dependencies import get_db_session

        session = _make_mock_session()
        member_row = MagicMock()
        member_row.team_id = team_a.id
        member_row.cnt = 3
        member_count_result = MagicMock()
        member_count_result.all = MagicMock(return_value=[member_row])
        session.execute = AsyncMock(return_value=member_count_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        admin_rls_client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch("modulo.api.routes.admin.list_teams", new=AsyncMock(return_value=page_result)),
                patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
                patch(
                    "modulo.api.routes.admin.count_owned_resources",
                    new=AsyncMock(return_value={team_a.id: 4, team_b.id: 2}),
                ),
            ):
                resp = admin_rls_client.get(self.URL)
        finally:
            admin_rls_client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 200
        data = resp.json()
        by_name = {item["name"]: item for item in data["items"]}
        assert by_name["Team A"]["member_count"] == 3
        assert by_name["Team A"]["owned_resource_count"] == 4
        assert by_name["Team B"]["owned_resource_count"] == 2
        assert by_name["Team A"]["updated_at"]

    def test_operator_returns_403(self, operator_rls_client: TestClient) -> None:
        resp = operator_rls_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)


class TestAdminUpdateTeamOptimisticLock:
    """PUT /api/v1/admin/teams/{id} with expected_updated_at — optimistic concurrency."""

    URL = "/api/v1/admin/teams"

    def _mock_team(self, team_id: uuid.UUID, name: str) -> MagicMock:
        t = MagicMock()
        t.id = team_id
        t.organisation_id = _ORG_ID
        t.name = name
        t.description = None
        t.account_id = _USER_ID
        t.created_at = _NOW
        t.updated_at = _NOW
        return t

    def test_stale_expected_updated_at_returns_409(self, admin_rls_client: TestClient) -> None:
        team = self._mock_team(uuid.uuid4(), "Current")

        async def _stale(*_a: object, **_k: object) -> None:
            raise HTTPException(
                status_code=409,
                detail="Team was modified by another request. Refresh and try again (optimistic lock mismatch).",
            )

        with (
            patch("modulo.api.routes.admin._assert_team_not_stale", new=_stale),
            patch("modulo.api.routes.admin.get_team_by_name", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
        ):
            resp = admin_rls_client.put(
                f"{self.URL}/{team.id}",
                json={"name": "Renamed", "expected_updated_at": "2024-01-01T00:00:00+00:00"},
            )
        assert resp.status_code == 409
        assert "optimistic lock" in resp.json()["detail"].lower()

    def test_matching_expected_updated_at_succeeds(self, admin_rls_client: TestClient) -> None:
        team = self._mock_team(uuid.uuid4(), "Updated")
        expected = team.updated_at.isoformat()
        with (
            patch("modulo.api.routes.admin.get_team_by_name", new=AsyncMock(return_value=None)),
            patch(
                "modulo.api.routes.admin.crud_update_team",
                new=AsyncMock(return_value=team),
            ),
            patch(
                "modulo.api.routes.admin._assert_team_not_stale",
                new=AsyncMock(return_value=None),
            ),
            patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()),
        ):
            resp = admin_rls_client.put(
                f"{self.URL}/{team.id}",
                json={"name": "Updated", "expected_updated_at": expected},
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
