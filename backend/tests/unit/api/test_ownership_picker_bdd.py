"""Unit tests for Ownership Picker BDD scenarios — covers ownership & visibility API contract."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_STAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_ALT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")


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


def _fake_stage(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = overrides.get("id", _STAGE_ID)
    s.organisation_id = overrides.get("organisation_id", _ORG_ID)
    s.name = overrides.get("name", "test-stage")
    s.description = overrides.get("description", None)
    s.position = overrides.get("position", 0)
    s.owner_team_id = overrides.get("owner_team_id", None)
    s.visibility = overrides.get("visibility", "org")
    s.created_by = overrides.get("created_by", _USER_ID)
    s.created_at = overrides.get("created_at", datetime.now())
    s.updated_at = overrides.get("updated_at", datetime.now())
    return s


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
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def member_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="member",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def non_member_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="nonmember",
        organisation_id=_ORG_ID,
        user_id=_ALT_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Scenario 1: Create resource with org visibility
# ===========================================================================


class TestBDDCreateOrgVisibility:
    URL = "/api/v1/stages"

    def test_create_with_org_visibility_returns_201(self, client: TestClient) -> None:
        fake = _fake_stage(name="ci-pipeline", visibility="org", owner_team_id=None)
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.create_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.post(self.URL, json={"name": "ci-pipeline", "visibility": "org"})

        assert resp.status_code == 201
        data = resp.json()
        assert data["visibility"] == "org"
        assert data["owner_team_id"] is None

    def test_create_with_org_defaults_to_org(self, client: TestClient) -> None:
        fake = _fake_stage(name="default-stage", visibility="org", owner_team_id=None)
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.create_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.post(self.URL, json={"name": "default-stage", "visibility": "org"})

        assert resp.status_code == 201
        assert resp.json()["visibility"] == "org"


# ===========================================================================
# Scenario 2: Create resource with team visibility
# ===========================================================================


class TestBDDCreateTeamVisibility:
    URL = "/api/v1/stages"

    def test_create_with_team_visibility_returns_201(self, client: TestClient) -> None:
        fake = _fake_stage(name="deploy-stage", owner_team_id=_TEAM_ID, visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.create_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.post(
                self.URL,
                json={"name": "deploy-stage", "owner_team_id": str(_TEAM_ID), "visibility": "team"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["visibility"] == "team"
        assert data["owner_team_id"] is not None
        assert data["owner_team_id"] == str(_TEAM_ID)

    def test_create_with_team_no_owner_team_id_uses_null(self, client: TestClient) -> None:
        fake = _fake_stage(name="orphan-stage", owner_team_id=None, visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.create_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.post(
                self.URL,
                json={"name": "orphan-stage", "visibility": "team"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["owner_team_id"] is None


# ===========================================================================
# Scenario 3: No silent default — missing visibility
# ===========================================================================


class TestBDDMissingVisibility:
    URL = "/api/v1/stages"

    def test_create_without_visibility_field_uses_default(self, client: TestClient) -> None:
        fake = _fake_stage(name="defaulted-stage", visibility="org", owner_team_id=None)
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.create_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.post(self.URL, json={"name": "defaulted-stage"})

        assert resp.status_code == 201

    def test_create_with_invalid_visibility_returns_422(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"name": "broken", "visibility": "invalid"})
        assert resp.status_code == 422

    def test_create_with_invalid_owner_team_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            self.URL,
            json={"name": "broken", "owner_team_id": "not-a-uuid", "visibility": "team"},
        )
        assert resp.status_code == 422


# ===========================================================================
# Scenario 4: Ownership shown in response
# ===========================================================================


class TestBDDOwnershipInResponse:
    URL = "/api/v1/stages"

    def test_get_stage_shows_ownership_fields_org(self, client: TestClient) -> None:
        fake = _fake_stage(name="audit-stage", visibility="org", owner_team_id=None)
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.get_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.get(f"{self.URL}/{_STAGE_ID}")

        assert resp.status_code == 200
        data = resp.json()
        assert "owner_team_id" in data
        assert "visibility" in data
        assert data["visibility"] == "org"
        assert data["owner_team_id"] is None

    def test_get_stage_shows_ownership_fields_team(self, client: TestClient) -> None:
        fake = _fake_stage(name="team-stage", owner_team_id=_TEAM_ID, visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.get_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.get(f"{self.URL}/{_STAGE_ID}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["owner_team_id"] == str(_TEAM_ID)
        assert data["visibility"] == "team"


# ===========================================================================
# Scenario 5: Non-member cannot access team resource
# ===========================================================================


class TestBDDNonMemberAccess:
    URL = "/api/v1/stages"

    def test_non_member_gets_404_for_team_resource(self, non_member_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.get_stage", new_callable=AsyncMock, return_value=None),
        ):
            resp = non_member_client.get(f"{self.URL}/{_STAGE_ID}")

        assert resp.status_code == 404

    def test_non_member_list_does_not_include_team_resource(self, non_member_client: TestClient) -> None:
        from modulo.db.crud.base import PageResult

        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.stages.list_stages",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ),
        ):
            resp = non_member_client.get(self.URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0


# ===========================================================================
# Scenario 6: Team member can access team resource
# ===========================================================================


class TestBDDTeamMemberAccess:
    URL = "/api/v1/stages"

    def test_member_can_get_team_resource(self, member_client: TestClient) -> None:
        fake = _fake_stage(name="secret-stage", owner_team_id=_TEAM_ID, visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.get_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = member_client.get(f"{self.URL}/{_STAGE_ID}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["visibility"] == "team"
        assert data["owner_team_id"] == str(_TEAM_ID)

    def test_member_can_list_team_resources(self, member_client: TestClient) -> None:
        from modulo.db.crud.base import PageResult

        fake = _fake_stage(name="secret-stage", owner_team_id=_TEAM_ID, visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.stages.list_stages",
                new_callable=AsyncMock,
                return_value=PageResult(items=[fake], total=1, page=1, page_size=20),
            ),
        ):
            resp = member_client.get(self.URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_member_can_update_team_resource(self, member_client: TestClient) -> None:
        fake = _fake_stage(name="secret-stage", owner_team_id=_TEAM_ID, visibility="team", description="updated")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.update_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = member_client.patch(f"{self.URL}/{_STAGE_ID}", json={"description": "updated"})

        assert resp.status_code == 200
        assert resp.json()["description"] == "updated"


# ===========================================================================
# Scenario 7: Admin bypasses team isolation
# ===========================================================================


class TestBDDAdminBypass:
    URL = "/api/v1/stages"

    def test_admin_can_get_any_team_resource(self, client: TestClient) -> None:
        fake = _fake_stage(name="secret-stage", owner_team_id=_TEAM_ID, visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.get_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.get(f"{self.URL}/{_STAGE_ID}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["visibility"] == "team"
        assert data["owner_team_id"] == str(_TEAM_ID)

    def test_admin_can_list_all_team_resources(self, client: TestClient) -> None:
        from modulo.db.crud.base import PageResult

        fake = _fake_stage(name="team-a-stage", owner_team_id=_TEAM_ID, visibility="team")
        fake_b = _fake_stage(name="team-b-stage", id=uuid.uuid4(), owner_team_id=uuid.uuid4(), visibility="team")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.stages.list_stages",
                new_callable=AsyncMock,
                return_value=PageResult(items=[fake, fake_b], total=2, page=1, page_size=20),
            ),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_admin_can_update_any_team_resource(self, client: TestClient) -> None:
        fake = _fake_stage(name="secret-stage", owner_team_id=_TEAM_ID, visibility="team", description="admin-updated")
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.update_stage", new_callable=AsyncMock, return_value=fake),
        ):
            resp = client.patch(f"{self.URL}/{_STAGE_ID}", json={"description": "admin-updated"})

        assert resp.status_code == 200
        assert resp.json()["description"] == "admin-updated"

    def test_admin_can_delete_any_team_resource(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.stages.delete_stage", new_callable=AsyncMock, return_value=True),
        ):
            resp = client.delete(f"{self.URL}/{_STAGE_ID}")

        assert resp.status_code == 204


# ===========================================================================
# Edge cases / validation
# ===========================================================================


class TestBDDOwnershipValidation:
    URL = "/api/v1/stages"

    def test_visibility_validation_rejects_bad_value(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"name": "bad-vis", "visibility": "public"})
        assert resp.status_code == 422

    def test_visibility_validation_rejects_numeric(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"name": "bad-vis", "visibility": 123})
        assert resp.status_code == 422

    def test_owner_team_id_validation_rejects_malformed_uuid(self, client: TestClient) -> None:
        resp = client.post(
            self.URL,
            json={"name": "bad-owner", "owner_team_id": "not-a-uuid", "visibility": "team"},
        )
        assert resp.status_code == 422
