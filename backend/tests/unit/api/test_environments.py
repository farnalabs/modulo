"""Unit tests for /api/v1/environments endpoints (environment profile CRUD)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


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


def _fake_profile(**overrides: Any) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PROFILE_ID)
    p.organisation_id = overrides.get("organisation_id", _ORG_ID)
    p.name = overrides.get("name", "test-profile")
    p.description = overrides.get("description", "A test profile")
    p.image_ref = overrides.get("image_ref", "python:3.12-slim")
    p.capabilities = overrides.get("capabilities", ["docker"])
    p.egress_policy = overrides.get("egress_policy", "allow_all")
    p.timeout_seconds = overrides.get("timeout_seconds", 3600)
    p.resource_limits_json = overrides.get("resource_limits", {})
    p.persistence_policy = overrides.get("persistence_policy", {})
    p.is_active = overrides.get("is_active", True)
    p.created_by = overrides.get("created_by", _USER_ID)
    p.created_at = None
    p.updated_at = None
    return p


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


class TestListProfiles:
    URL = "/api/v1/environments"

    def test_list_profiles_returns_paginated(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch("modulo.api.routes.environments.list_environment_profiles") as mock_list,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=[fake], total=1, page=1, page_size=20)
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "test-profile"
        assert data["items"][0]["image_ref"] == "python:3.12-slim"

    def test_list_profiles_empty(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.list_environment_profiles") as mock_list,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=[], total=0, page=1, page_size=20)
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_profiles_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)


class TestCreateProfile:
    URL = "/api/v1/environments"

    PAYLOAD: ClassVar[dict[str, Any]] = {
        "name": "new-env",
        "image_ref": "ubuntu:22.04",
        "capabilities": ["docker", "gpu"],
        "egress_policy": "allow_all",
        "timeout_seconds": 7200,
    }

    def test_create_profile_returns_201(self, client: TestClient) -> None:
        fake = _fake_profile(name="new-env", image_ref="ubuntu:22.04", capabilities=["docker", "gpu"])
        with (
            patch("modulo.api.routes.environments.create_environment_profile") as mock_create,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_create.return_value = fake
            resp = client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "new-env"
        assert data["image_ref"] == "ubuntu:22.04"
        assert data["capabilities"] == ["docker", "gpu"]

    def test_create_profile_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code in (401, 403)

    def test_create_profile_missing_required_fields(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"name": "incomplete"})
        assert resp.status_code == 422


class TestGetProfile:
    URL = "/api/v1/environments"

    def test_get_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch("modulo.api.routes.environments.get_environment_profile") as mock_get,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_get.return_value = fake
            resp = client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-profile"
        assert data["image_ref"] == "python:3.12-slim"

    def test_get_profile_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.get_environment_profile") as mock_get,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_get_profile_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code in (401, 403)


class TestUpdateProfile:
    URL = "/api/v1/environments"

    def test_update_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile(name="updated-name")
        with (
            patch("modulo.api.routes.environments.update_environment_profile") as mock_update,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_update.return_value = fake
            resp = client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "updated-name"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "updated-name"

    def test_update_profile_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.update_environment_profile") as mock_update,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_update.return_value = None
            resp = client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "nope"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_update_profile_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "x"})
        assert resp.status_code in (401, 403)

    def test_update_profile_invalid_egress_policy(self, client: TestClient) -> None:
        resp = client.patch(f"{self.URL}/{_PROFILE_ID}", json={"egress_policy": "bogus"})
        assert resp.status_code == 422


class TestDeleteProfile:
    URL = "/api/v1/environments"

    def test_delete_profile_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.delete_environment_profile") as mock_delete,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_delete.return_value = True
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 204

    def test_delete_profile_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.delete_environment_profile") as mock_delete,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_delete.return_value = False
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_delete_profile_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code in (401, 403)


class TestProfileTestEndpoint:
    URL = "/api/v1/environments"

    def test_profile_test_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code in (401, 403)

    def test_profile_test_profile_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.get_environment_profile") as mock_get,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"
