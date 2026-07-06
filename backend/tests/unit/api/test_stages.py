"""Unit tests for /api/v1/stages endpoints."""

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

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_STAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


def _make_stage(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.id = overrides.get("id", _STAGE_ID)
    s.organisation_id = overrides.get("organisation_id", _ORG_ID)
    s.name = overrides.get("name", "Test Stage")
    s.description = overrides.get("description")
    s.position = overrides.get("position", 0)
    s.owner_team_id = overrides.get("owner_team_id")
    s.visibility = overrides.get("visibility", "org")
    s.created_by = overrides.get("created_by", _USER_ID)
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
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


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListStages:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_stage()], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.stages.list_stages", return_value=page_result),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/stages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_returns_empty_when_no_stages(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch("modulo.api.routes.stages.list_stages", return_value=page_result),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/stages")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/stages")
        assert resp.status_code in (401, 403)


class TestCreateStage:
    def test_returns_201(self, client: TestClient) -> None:
        stage = _make_stage(name="New Stage")
        with (
            patch("modulo.api.routes.stages.create_stage", return_value=stage) as create,
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/stages", json={"name": "New Stage"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "New Stage"
        create.assert_awaited_once()

    def test_with_all_fields(self, client: TestClient) -> None:
        stage = _make_stage(
            name="Full Stage", description="desc", position=2, owner_team_id=_TEAM_ID, visibility="team"
        )
        with (
            patch("modulo.api.routes.stages.create_stage", return_value=stage),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.post(
                "/api/v1/stages",
                json={
                    "name": "Full Stage",
                    "description": "desc",
                    "position": 2,
                    "owner_team_id": str(_TEAM_ID),
                    "visibility": "team",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Full Stage"
        assert body["description"] == "desc"
        assert body["position"] == 2
        assert body["owner_team_id"] == str(_TEAM_ID)
        assert body["visibility"] == "team"

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/stages", json={"name": ""})
        assert resp.status_code == 422

    def test_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/stages", json={})
        assert resp.status_code == 422

    def test_invalid_visibility_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/stages", json={"name": "Test", "visibility": "invalid"})
        assert resp.status_code == 422

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/api/v1/stages", json={"name": "Test"})
        assert resp.status_code in (401, 403)


class TestGetStage:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.get_stage", return_value=_make_stage()),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/stages/{_STAGE_ID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_STAGE_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.get_stage", return_value=None),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/stages/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateStage:
    def test_returns_200(self, client: TestClient) -> None:
        stage = _make_stage(name="Updated")
        with (
            patch("modulo.api.routes.stages.update_stage", return_value=stage),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/stages/{_STAGE_ID}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_partial_update(self, client: TestClient) -> None:
        stage = _make_stage(name="Original", description="New description")
        with (
            patch("modulo.api.routes.stages.update_stage", return_value=stage),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/stages/{_STAGE_ID}", json={"description": "New description"})
        assert resp.status_code == 200
        assert resp.json()["description"] == "New description"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.update_stage", return_value=None),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/stages/{uuid.uuid4()}", json={"name": "x"})
        assert resp.status_code == 404

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.patch(f"/api/v1/stages/{_STAGE_ID}", json={"name": ""})
        assert resp.status_code == 422


class TestDeleteStage:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.delete_stage", return_value=True),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/stages/{_STAGE_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.delete_stage", return_value=False),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/stages/{uuid.uuid4()}")
        assert resp.status_code == 404
