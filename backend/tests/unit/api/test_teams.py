"""Unit tests for /api/v1/teams endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_MEMBERSHIP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


class _TeamPlan:
    """Stub plan context that enables all team features for tests."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []


def _make_team(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEAM_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Test Team")
    t.description = overrides.get("description")
    t.account_id = overrides.get("account_id", _USER_ID)
    t.notification_endpoints = overrides.get("notification_endpoints", [])
    t.created_at = _NOW
    t.updated_at = _NOW
    return t


def _make_membership(**overrides: object) -> MagicMock:
    m = MagicMock()
    m.id = overrides.get("id", _MEMBERSHIP_ID)
    m.organisation_id = overrides.get("organisation_id", _ORG_ID)
    m.team_id = overrides.get("team_id", _TEAM_ID)
    m.account_id = overrides.get("account_id", _USER_ID)
    m.role = overrides.get("role", "viewer")
    m.created_at = _NOW
    return m


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    scalar_result = MagicMock()
    scalar_result.scalar = MagicMock(return_value=0)
    session.execute = AsyncMock(return_value=scalar_result)
    return session


_TEAM_BODY = {"name": "New Team"}


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    with patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
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
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListTeams:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_team()], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.teams.list_teams", return_value=page_result),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/teams")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_returns_empty_when_no_teams(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch("modulo.api.routes.teams.list_teams", return_value=page_result),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/teams")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/teams")
        assert resp.status_code in (401, 403)


class TestCreateTeam:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.create_team",
                return_value=_make_team(name="New Team"),
            ),
            patch(
                "modulo.api.routes.teams.get_team_by_name",
                return_value=None,
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/teams", json=_TEAM_BODY)
        assert resp.status_code == 201
        assert resp.json()["name"] == "New Team"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post("/api/v1/teams", json=_TEAM_BODY)
        assert resp.status_code == 403

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/teams", json={"name": ""})
        assert resp.status_code == 422

    def test_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/teams", json={})
        assert resp.status_code == 422


class TestGetTeam:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.get_team",
                return_value=_make_team(),
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_TEAM_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.get_team", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateTeam:
    def test_returns_200(self, client: TestClient) -> None:
        team = _make_team(name="Updated")
        with (
            patch("modulo.api.routes.teams.update_team", return_value=team),
            patch("modulo.api.routes.teams.get_team_by_name", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.update_team", return_value=None),
            patch("modulo.api.routes.teams.get_team_by_name", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/teams/{uuid.uuid4()}", json={"name": "x"})
        assert resp.status_code == 404

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": ""})
        assert resp.status_code == 422

    def test_duplicate_name_returns_409(self, client: TestClient) -> None:
        existing = _make_team(id=uuid.uuid4(), name="Existing")
        with (
            patch("modulo.api.routes.teams.get_team_by_name", return_value=existing),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": "Existing"})
        assert resp.status_code == 409

    def test_same_name_same_team_allowed(self, client: TestClient) -> None:
        team = _make_team(name="Same Name")
        with (
            patch("modulo.api.routes.teams.update_team", return_value=team),
            patch("modulo.api.routes.teams.get_team_by_name", return_value=team),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": "Same Name"})
        assert resp.status_code == 200


class TestDeleteTeam:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=False),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_emits_team_deleted_audit(self, client: TestClient) -> None:
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 204
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "team_deleted"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "team"
        assert kwargs["resource_id"] == _TEAM_ID
        assert kwargs["payload_json"] == {"team_id": str(_TEAM_ID)}


class TestAddMember:
    def test_returns_201(self, client: TestClient) -> None:
        target_account = MagicMock()
        target_account.id = _USER_ID
        target_membership = MagicMock()
        target_membership.role = "admin"
        with (
            patch(
                "modulo.api.routes.teams.add_team_member",
                return_value=_make_membership(),
            ),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=target_account)),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                new=AsyncMock(return_value=target_membership),
            ),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "viewer"},
            )
        assert resp.status_code == 201

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": str(_USER_ID), "role": "viewer"},
        )
        assert resp.status_code == 403

    def test_invalid_user_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": "not-a-uuid", "role": "viewer"},
        )
        assert resp.status_code == 422

    def test_invalid_role_pattern_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": str(_USER_ID), "role": "superadmin"},
        )
        assert resp.status_code == 422

    def test_role_exceeds_org_role_returns_422(self, client: TestClient) -> None:
        target_account = MagicMock()
        target_account.id = _USER_ID
        target_membership = MagicMock()
        target_membership.role = "viewer"
        with (
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=target_account)),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                new=AsyncMock(return_value=target_membership),
            ),
            patch("modulo.api.routes.teams.add_team_member"),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "operator"},
            )
        assert resp.status_code == 422
        data = resp.json()
        assert "exceeds" in data["detail"].lower()

    def test_role_within_org_role_succeeds(self, client: TestClient) -> None:
        target_account = MagicMock()
        target_account.id = _USER_ID
        target_membership = MagicMock()
        target_membership.role = "admin"
        with (
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=target_account)),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                new=AsyncMock(return_value=target_membership),
            ),
            patch(
                "modulo.api.routes.teams.add_team_member",
                new=AsyncMock(return_value=_make_membership(role="operator")),
            ),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "operator"},
            )
        assert resp.status_code == 201

    def test_user_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(uuid.uuid4()), "role": "viewer"},
            )
        assert resp.status_code == 404

    def test_emits_team_member_added_audit(self, client: TestClient) -> None:
        target_account = MagicMock()
        target_account.id = _USER_ID
        target_membership = MagicMock()
        target_membership.role = "admin"
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch(
                "modulo.api.routes.teams.add_team_member",
                return_value=_make_membership(role="viewer"),
            ),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=target_account)),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                new=AsyncMock(return_value=target_membership),
            ),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "viewer"},
            )
        assert resp.status_code == 201
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "team_member_added"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "team_membership"
        assert kwargs["resource_id"] == _MEMBERSHIP_ID
        assert kwargs["payload_json"] == {
            "team_id": str(_TEAM_ID),
            "user_id": str(_USER_ID),
            "role": "viewer",
        }

    def test_audit_failure_does_not_block_add(self, client: TestClient) -> None:
        target_account = MagicMock()
        target_account.id = _USER_ID
        target_membership = MagicMock()
        target_membership.role = "admin"

        async def _raise_audit(*_a: object, **_k: object) -> object:
            raise RuntimeError("audit boom")

        with (
            patch(
                "modulo.api.routes.teams.add_team_member",
                return_value=_make_membership(),
            ),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=target_account)),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                new=AsyncMock(return_value=target_membership),
            ),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "viewer"},
            )
        assert resp.status_code == 201


class TestListMembers:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_membership()], total=1, page=1, page_size=20)
        with (
            patch(
                "modulo.api.routes.teams.list_team_members",
                return_value=page_result,
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{_TEAM_ID}/members")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_empty_members(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch(
                "modulo.api.routes.teams.list_team_members",
                return_value=page_result,
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{_TEAM_ID}/members")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestRemoveMember:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=_make_membership(),
            ),
            patch("modulo.api.routes.teams.remove_team_member", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=None,
            ),
            patch("modulo.api.routes.teams.remove_team_member", return_value=False),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_emits_team_member_removed_audit(self, client: TestClient) -> None:
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=_make_membership(role="operator"),
            ),
            patch("modulo.api.routes.teams.remove_team_member", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")
        assert resp.status_code == 204
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "team_member_removed"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "team_membership"
        assert kwargs["resource_id"] == _MEMBERSHIP_ID
        assert kwargs["payload_json"] == {
            "team_id": str(_TEAM_ID),
            "user_id": str(_USER_ID),
            "role": "operator",
        }

    def test_not_found_does_not_emit_audit(self, client: TestClient) -> None:
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=None,
            ),
            patch("modulo.api.routes.teams.remove_team_member", return_value=False),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{uuid.uuid4()}")
        assert resp.status_code == 404
        audit.assert_not_awaited()

    def test_audit_failure_does_not_block_removal(self, client: TestClient) -> None:
        async def _raise_audit(*_a: object, **_k: object) -> object:
            raise RuntimeError("audit boom")

        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=_make_membership(),
            ),
            patch("modulo.api.routes.teams.remove_team_member", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")
        assert resp.status_code == 204


class TestChangeMemberRole:
    def test_returns_200(self, client: TestClient) -> None:
        existing = _make_membership(role="viewer")
        updated = _make_membership(role="operator")
        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.get_membership", return_value=existing),
            patch("modulo.api.routes.teams.update_member_role", return_value=updated),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
                json={"role": "operator"},
            )
        assert resp.status_code == 200
        assert resp.json()["role"] == "operator"

    def test_membership_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.get_membership", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}/members/{uuid.uuid4()}",
                json={"role": "operator"},
            )
        assert resp.status_code == 404

    def test_invalid_role_returns_422(self, client: TestClient) -> None:
        resp = client.patch(
            f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
            json={"role": "superadmin"},
        )
        assert resp.status_code == 422

    def test_emits_team_member_role_changed_audit(self, client: TestClient) -> None:
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.get_membership", return_value=_make_membership(role="viewer")),
            patch("modulo.api.routes.teams.update_member_role", return_value=_make_membership(role="operator")),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
                json={"role": "operator"},
            )
        assert resp.status_code == 200
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "team_member_role_changed"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "team_membership"
        assert kwargs["resource_id"] == _MEMBERSHIP_ID
        assert kwargs["payload_json"] == {
            "team_id": str(_TEAM_ID),
            "user_id": str(_USER_ID),
            "old_role": "viewer",
            "new_role": "operator",
        }

    def test_not_found_does_not_emit_audit(self, client: TestClient) -> None:
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.get_membership", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}/members/{uuid.uuid4()}",
                json={"role": "operator"},
            )
        assert resp.status_code == 404
        audit.assert_not_awaited()

    def test_audit_failure_does_not_block_role_change(self, client: TestClient) -> None:
        async def _raise_audit(*_a: object, **_k: object) -> object:
            raise RuntimeError("audit boom")

        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.get_membership", return_value=_make_membership(role="viewer")),
            patch("modulo.api.routes.teams.update_member_role", return_value=_make_membership(role="operator")),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
                json={"role": "operator"},
            )
        assert resp.status_code == 200
        assert resp.json()["role"] == "operator"


class TestAdminCreateTeam:
    def test_admin_creates_team_returns_201(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin.create_team",
                return_value=_make_team(name="Admin Team"),
            ),
            patch(
                "modulo.api.routes.admin.get_team_by_name",
                return_value=None,
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/admin/teams",
                json={"name": "Admin Team"},
            )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Admin Team"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(
            "/api/v1/admin/teams",
            json={"name": "Admin Team"},
        )
        assert resp.status_code == 403

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/admin/teams", json={"name": ""})
        assert resp.status_code == 422


class TestMyTeams:
    """GET /api/v1/teams/my — profile panel "My Teams" section."""

    def test_returns_memberships_with_team_names(self, client: TestClient) -> None:
        memberships = [
            MagicMock(team_id=_TEAM_ID, account_id=_USER_ID, role="operator"),
            MagicMock(team_id=uuid.uuid4(), account_id=_USER_ID, role="viewer"),
        ]
        team_ids = [m.team_id for m in memberships]
        team_rows = [(team_ids[0], "Engineering"), (team_ids[1], "Design")]

        mock_session = _make_mock_session()
        row_result = MagicMock()
        row_result.all = MagicMock(return_value=team_rows)
        mock_session.execute = AsyncMock(return_value=row_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        from modulo.api.dependencies import get_db_session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch(
                    "modulo.api.routes.teams.list_team_memberships_for_account",
                    new=AsyncMock(return_value=memberships),
                ),
                patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            ):
                resp = client.get("/api/v1/teams/my")
        finally:
            client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["team_id"] == str(team_ids[0])
        assert data[0]["team_name"] == "Engineering"
        assert data[0]["role"] == "operator"
        assert data[1]["team_name"] == "Design"

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/teams/my")
        assert resp.status_code in (401, 403)


class TestUpdateTeamOptimisticLock:
    """PATCH /api/v1/teams/{id} with expected_updated_at — optimistic concurrency."""

    def test_stale_expected_updated_at_returns_409(self, client: TestClient) -> None:
        team = _make_team(name="Current Name")
        with (
            patch("modulo.api.routes.teams.get_team", return_value=team),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}",
                json={"name": "Renamed", "expected_updated_at": "2024-01-01T00:00:00+00:00"},
            )
        assert resp.status_code == 409
        assert "optimistic lock" in resp.json()["detail"].lower()

    def test_matching_expected_updated_at_succeeds(self, client: TestClient) -> None:
        team = _make_team(name="Updated")
        expected = team.updated_at.isoformat()
        with (
            patch("modulo.api.routes.teams.get_team", return_value=team),
            patch("modulo.api.routes.teams.update_team", return_value=team),
            patch("modulo.api.routes.teams.get_team_by_name", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}",
                json={"name": "Updated", "expected_updated_at": expected},
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_no_expected_updated_at_still_succeeds(self, client: TestClient) -> None:
        team = _make_team(name="Renamed")
        with (
            patch("modulo.api.routes.teams.update_team", return_value=team),
            patch("modulo.api.routes.teams.get_team_by_name", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": "Renamed"})
        assert resp.status_code == 200


class TestRemoveMemberLastOperatorGuard:
    """Cannot remove the last operator while other members remain."""

    def test_removing_last_operator_returns_409(self, client: TestClient) -> None:
        # session.execute returns 0 for other operators (count scalar).
        mock_session = _make_mock_session()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(side_effect=[1, 0])  # other members=1, other operators=0
        mock_session.execute = AsyncMock(return_value=count_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        from modulo.api.dependencies import get_db_session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch(
                    "modulo.api.routes.teams.get_membership",
                    return_value=_make_membership(role="operator"),
                ),
                patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            ):
                resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")
        finally:
            client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 409
        assert "last operator" in resp.json()["detail"].lower()

    def test_removing_operator_with_another_operator_succeeds(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(side_effect=[2, 1])  # other members=2, other operators=1
        mock_session.execute = AsyncMock(return_value=count_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        from modulo.api.dependencies import get_db_session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch(
                    "modulo.api.routes.teams.get_membership",
                    return_value=_make_membership(role="operator"),
                ),
                patch("modulo.api.routes.teams.remove_team_member", return_value=True),
                patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            ):
                resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")
        finally:
            client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 204

    def test_self_removal_of_non_last_operator_succeeds(self, client: TestClient) -> None:
        """A team operator removing their own membership succeeds when another operator remains."""
        mock_session = _make_mock_session()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(side_effect=[2, 1])
        mock_session.execute = AsyncMock(return_value=count_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        from modulo.api.dependencies import get_db_session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch(
                    "modulo.api.routes.teams.get_membership",
                    return_value=_make_membership(role="operator", account_id=_USER_ID),
                ),
                patch("modulo.api.routes.teams.remove_team_member", return_value=True),
                patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            ):
                resp = client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}")
        finally:
            client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 204


class TestChangeMemberRoleLastOperatorGuard:
    def test_demoting_last_operator_returns_409(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(side_effect=[1, 0])
        mock_session.execute = AsyncMock(return_value=count_result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        from modulo.api.dependencies import get_db_session

        client.app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
                patch(
                    "modulo.api.routes.teams.get_membership",
                    return_value=_make_membership(role="operator"),
                ),
                patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
                patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
            ):
                resp = client.patch(
                    f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
                    json={"role": "viewer"},
                )
        finally:
            client.app.dependency_overrides[get_db_session] = None
        assert resp.status_code == 409
        assert "last operator" in resp.json()["detail"].lower()

    def test_role_change_race_membership_removed_returns_404(self, client: TestClient) -> None:
        """Role change racing a concurrent removal: update_member_role returns None -> 404."""
        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=_make_membership(role="viewer"),
            ),
            patch("modulo.api.routes.teams.update_member_role", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}",
                json={"role": "viewer"},
            )
        assert resp.status_code == 404


class TestAddMemberDeletedTeam:
    def test_add_member_to_soft_deleted_team_returns_404(self, client: TestClient) -> None:
        """Adding a member during/after team deletion is rejected (team gone)."""
        with (
            patch("modulo.api.routes.teams.get_team", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "viewer"},
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Team not found"
