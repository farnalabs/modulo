"""Additional unit tests for environment profile API routes — BDD-style coverage."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
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
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


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


@pytest.fixture()
def alt_org_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="otheruser",
        organisation_id=_ALT_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="viewer",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Create Profile
# ===========================================================================


class TestBDDCreateProfile:
    URL = "/api/v1/environments"

    def test_create_name_too_short(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"name": "", "image_ref": "python:3.12-slim"})
        assert resp.status_code == 422

    def test_create_invalid_timeout_below_min(self, client: TestClient) -> None:
        resp = client.post(
            self.URL,
            json={"name": "test", "image_ref": "python:3.12-slim", "timeout_seconds": 30},
        )
        assert resp.status_code == 422

    def test_create_invalid_timeout_above_max(self, client: TestClient) -> None:
        resp = client.post(
            self.URL,
            json={"name": "test", "image_ref": "python:3.12-slim", "timeout_seconds": 90000},
        )
        assert resp.status_code == 422

    def test_create_invalid_egress_policy(self, client: TestClient) -> None:
        resp = client.post(
            self.URL,
            json={
                "name": "test",
                "image_ref": "python:3.12-slim",
                "egress_policy": "bogus",
            },
        )
        assert resp.status_code == 422


# ===========================================================================
# List Profiles
# ===========================================================================


class TestBDDListProfiles:
    URL = "/api/v1/environments"

    def test_list_empty(self, client: TestClient) -> None:
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

    def test_list_paginated_with_large_page(self, client: TestClient) -> None:
        items = [_fake_profile(name=f"p-{i}") for i in range(50)]
        with (
            patch("modulo.api.routes.environments.list_environment_profiles") as mock_list,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=items, total=50, page=1, page_size=50)
            resp = client.get(f"{self.URL}?page_size=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 50
        assert len(data["items"]) == 50


# ===========================================================================
# Get Profile
# ===========================================================================


class TestBDDGetProfile:
    URL = "/api/v1/environments"

    def test_get_invalid_uuid_returns_422(self, client: TestClient) -> None:
        resp = client.get(f"{self.URL}/not-a-uuid")
        assert resp.status_code == 422


# ===========================================================================
# Update Profile
# ===========================================================================


class TestBDDUpdateProfile:
    URL = "/api/v1/environments"

    def test_update_partial_single_field(self, client: TestClient) -> None:
        fake = _fake_profile(description="Updated desc")
        with (
            patch("modulo.api.routes.environments.update_environment_profile") as mock_update,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_update.return_value = fake
            resp = client.patch(
                f"{self.URL}/{_PROFILE_ID}",
                json={"description": "Updated desc"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "Updated desc"

    def test_update_all_fields(self, client: TestClient) -> None:
        fake = _fake_profile(
            name="full-update",
            description="Full update",
            image_ref="ubuntu:24.04",
            capabilities=["docker", "gpu", "network"],
            egress_policy="deny_all",
            timeout_seconds=7200,
        )
        with (
            patch("modulo.api.routes.environments.update_environment_profile") as mock_update,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_update.return_value = fake
            resp = client.patch(
                f"{self.URL}/{_PROFILE_ID}",
                json={
                    "name": "full-update",
                    "description": "Full update",
                    "image_ref": "ubuntu:24.04",
                    "capabilities": ["docker", "gpu", "network"],
                    "egress_policy": "deny_all",
                    "timeout_seconds": 7200,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "full-update"
        assert data["image_ref"] == "ubuntu:24.04"
        assert data["capabilities"] == ["docker", "gpu", "network"]
        assert data["egress_policy"] == "deny_all"
        assert data["timeout_seconds"] == 7200


# ===========================================================================
# Delete Profile
# ===========================================================================


class TestBDDDeleteProfile:
    URL = "/api/v1/environments"

    def test_delete_already_deleted_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.delete_environment_profile") as mock_delete,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_delete.return_value = False
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404

    def test_delete_with_invalid_id_returns_422(self, client: TestClient) -> None:
        resp = client.delete(f"{self.URL}/bad-uuid")
        assert resp.status_code == 422


# ===========================================================================
# Test Profile (sandbox test endpoint)
# ===========================================================================


class TestBDDTestProfile:
    URL = "/api/v1/environments"

    def test_test_profile_success_returns_sse(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch("modulo.api.routes.environments.get_environment_profile") as mock_get,
            patch("modulo.api.routes.environments._sandbox_test_stream") as mock_stream,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_get.return_value = fake
            mock_stream.return_value.__aiter__.return_value = iter(
                [
                    'data: {"event": "provisioning", "detail": "Creating sandbox..."}\n\n',
                    'data: {"event": "destroyed", "detail": "Sandbox destroyed"}\n\n',
                ]
            )
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "provisioning" in resp.text
        assert "destroyed" in resp.text

    def test_test_profile_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.get_environment_profile") as mock_get,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_test_profile_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code in (401, 403)


# ===========================================================================
# Workspace Lease
# ===========================================================================


class TestBDDWorkspaceLease:
    """Test workspace lease lifecycle via the runs endpoint."""

    RUN_URL = "/api/v1/runs"

    def test_get_workspace_lease_by_run_id(self, client: TestClient) -> None:
        run_id = uuid.uuid4()
        lease = MagicMock()
        lease.id = uuid.uuid4()
        lease.organisation_id = _ORG_ID
        lease.environment_profile_id = _PROFILE_ID
        lease.run_id = run_id
        lease.provider_ref = "ws-001"
        lease.status = "active"
        lease.started_at = None
        lease.expires_at = None
        lease.resource_usage_json = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = lease

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.execute = AsyncMock(return_value=mock_result)

        async def _override():
            yield mock_session

        old_override = app.dependency_overrides.get(get_db_session)
        app.dependency_overrides[get_db_session] = _override
        try:
            with patch("modulo.api.routes.runs.set_rls_org"):
                resp = client.get(f"/api/v1/runs/{run_id}/workspace-lease")
            assert resp.status_code == 200
        finally:
            if old_override is not None:
                app.dependency_overrides[get_db_session] = old_override
            else:
                del app.dependency_overrides[get_db_session]

    def test_get_workspace_lease_by_run_id_not_found(self, client: TestClient) -> None:
        run_id = uuid.uuid4()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.execute = AsyncMock(return_value=mock_result)

        async def _override():
            yield mock_session

        old_override = app.dependency_overrides.get(get_db_session)
        app.dependency_overrides[get_db_session] = _override
        try:
            with patch("modulo.api.routes.runs.set_rls_org"):
                resp = client.get(f"/api/v1/runs/{run_id}/workspace-lease")
            assert resp.status_code == 200
            assert resp.json() is None
        finally:
            if old_override is not None:
                app.dependency_overrides[get_db_session] = old_override
            else:
                del app.dependency_overrides[get_db_session]


# ===========================================================================
# Cross-org isolation
# ===========================================================================


class TestBDDEnvCrossOrgIsolation:
    URL = "/api/v1/environments"

    def test_org_b_cannot_list_org_a_profiles(self, alt_org_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.list_environment_profiles") as mock_list,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=[], total=0, page=1, page_size=20)
            resp = alt_org_client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_org_b_cannot_get_org_a_profile(self, alt_org_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.get_environment_profile") as mock_get,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = alt_org_client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_org_b_cannot_update_org_a_profile(self, alt_org_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.update_environment_profile") as mock_update,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_update.return_value = None
            resp = alt_org_client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "hacked"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_org_b_cannot_delete_org_a_profile(self, alt_org_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.environments.delete_environment_profile") as mock_delete,
            patch("modulo.api.routes.environments.set_rls_org"),
        ):
            mock_delete.return_value = False
            resp = alt_org_client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"
