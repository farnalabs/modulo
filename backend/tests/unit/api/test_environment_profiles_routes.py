"""Router-level unit tests for /api/v1/environment-profiles (CRUD + sandbox test).

The `/environment-profiles` router is the single surviving Environment Profiles
surface (FAR-551 collapsed the duplicate `/api/v1/environments` router into it).
These tests exercise the router in isolation with a mocked session + CRUD layer.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")

_ROUTES = "modulo.api.routes.environment_profiles"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    authz_result = MagicMock()
    authz_result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=authz_result)
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
    p.provider_type = overrides.get("provider_type", "local_docker")
    p.image_ref = overrides.get("image_ref", "python:3.12-slim")
    p.capabilities = overrides.get("capabilities", ["docker"])
    p.capabilities_json = overrides.get("capabilities", ["docker"])
    p.config_json = overrides.get("config_json", {})
    p.egress_policy = overrides.get("egress_policy", "allow_all")
    p.network_policy = overrides.get("network_policy", "outbound")
    p.initialisation_strategy = overrides.get("initialisation_strategy", "git_clone")
    p.secret_refs_json = overrides.get("secret_refs", [])
    p.timeout_seconds = overrides.get("timeout_seconds", 3600)
    p.resource_limits_json = overrides.get("resource_limits", {})
    p.persistence_policy = overrides.get("persistence_policy", "ephemeral")
    p.status = overrides.get("status", "active")
    p.visibility = overrides.get("visibility", "org")
    p.owner_team_id = overrides.get("owner_team_id")
    p.is_active = overrides.get("is_active", True)
    p.created_by = overrides.get("created_by", _USER_ID)
    p.created_at = overrides.get("created_at", datetime(2026, 1, 1, tzinfo=UTC))
    p.updated_at = overrides.get("updated_at", datetime(2026, 1, 1, tzinfo=UTC))
    return p


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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
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


_ENV_AUTH_CASES = [
    ("GET", "/api/v1/environment-profiles"),
    ("POST", "/api/v1/environment-profiles"),
    ("GET", f"/api/v1/environment-profiles/{_PROFILE_ID}"),
    ("PUT", f"/api/v1/environment-profiles/{_PROFILE_ID}"),
    ("DELETE", f"/api/v1/environment-profiles/{_PROFILE_ID}"),
    ("POST", f"/api/v1/environment-profiles/{_PROFILE_ID}/test"),
]


@pytest.mark.parametrize(("method", "url"), _ENV_AUTH_CASES, ids=["list", "create", "get", "update", "delete", "test"])
def test_endpoints_unauthenticated(unauth_client: TestClient, method: str, url: str) -> None:
    resp = getattr(unauth_client, method.lower())(url)
    assert resp.status_code in (401, 403), f"Expected 401/403 for {method} {url}, got {resp.status_code}"


class TestListProfiles:
    URL = "/api/v1/environment-profiles"

    def test_list_profiles_returns_paginated(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.list_environment_profiles") as mock_list,
            patch(f"{_ROUTES}.set_rls_org"),
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
            patch(f"{_ROUTES}.list_environment_profiles") as mock_list,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=[], total=0, page=1, page_size=20)
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert not data["items"]


class TestCreateProfile:
    URL = "/api/v1/environment-profiles"

    PAYLOAD: ClassVar[dict[str, Any]] = {
        "name": "new-env",
        "image_ref": "ubuntu:22.04",
        "capabilities": ["docker", "gpu"],
    }

    def test_create_profile_returns_201(self, client: TestClient) -> None:
        fake = _fake_profile(name="new-env", image_ref="ubuntu:22.04", capabilities=["docker", "gpu"])
        with (
            patch(f"{_ROUTES}.create_environment_profile") as mock_create,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_create.return_value = fake
            resp = client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "new-env"
        assert data["image_ref"] == "ubuntu:22.04"
        assert data["capabilities"] == ["docker", "gpu"]

    def test_create_profile_with_defaults(self, client: TestClient) -> None:
        fake = _fake_profile(name="incomplete")
        with (
            patch(f"{_ROUTES}.create_environment_profile") as mock_create,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_create.return_value = fake
            resp = client.post(self.URL, json={"name": "incomplete"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "incomplete"
        assert data["status"] == "active"

    def test_create_profile_conflict(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.create_environment_profile") as mock_create,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_create.side_effect = IntegrityError("mock", "mock", "mock")
            resp = client.post(self.URL, json={"name": "dup"})
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestGetProfile:
    URL = "/api/v1/environment-profiles"

    def test_get_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = fake
            resp = client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-profile"
        assert data["image_ref"] == "python:3.12-slim"

    def test_get_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"


class TestUpdateProfile:
    URL = "/api/v1/environment-profiles"

    def test_update_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile(name="updated-name")
        with (
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.return_value = fake
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"name": "updated-name"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "updated-name"

    def test_update_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.return_value = None
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"name": "nope"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_update_profile_conflict(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.get_environment_profile"),
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.side_effect = IntegrityError("mock", "mock", "mock")
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"name": "dup"})
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestDeleteProfile:
    URL = "/api/v1/environment-profiles"

    def test_delete_profile_returns_204(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.soft_delete_environment_profile") as mock_delete,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_delete.return_value = _fake_profile()
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 204

    def test_delete_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.soft_delete_environment_profile") as mock_delete,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_delete.return_value = None
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"


class TestRestoreProfile:
    URL = "/api/v1/environment-profiles"

    def test_restore_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.restore_environment_profile") as mock_restore,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_restore.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/restore")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-profile"

    def test_restore_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.restore_environment_profile") as mock_restore,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_restore.return_value = None
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/restore")
        assert resp.status_code == 404


class TestProfileTestEndpoint:
    URL = "/api/v1/environment-profiles"

    def test_profile_test_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_profile_test_streams_sse(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "provisioning" in resp.text
