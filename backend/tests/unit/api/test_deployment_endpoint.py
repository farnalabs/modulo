"""Unit tests for the deployment info endpoint.

FAR-880: the endpoint now requires a system admin principal
(``require_system_permission("system.config.manage")``).
"""

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="",
    )


def _system_admin_principal() -> dict[str, Any]:
    return {
        "username": "sysadmin",
        "organisation_id": _ORG_ID,
        "account_id": _USER_ID,
        "org_role": "admin",
        "is_system_admin": True,
    }


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(**_system_admin_principal())
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDeploymentInfo:
    def test_unauthenticated_returns_4xx(self, client: TestClient) -> None:
        app.dependency_overrides.pop(get_current_user, None)
        try:
            resp = client.get("/api/v1/deployment")
        finally:
            app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(**_system_admin_principal())
        assert resp.status_code in (401, 403)

    def test_non_system_admin_returns_403(self, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="orgadmin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
            is_system_admin=False,
        )
        try:
            resp = client.get("/api/v1/deployment")
        finally:
            app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(**_system_admin_principal())
        assert resp.status_code == 403
        assert "system admin" in resp.json()["detail"]

    def test_get_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        assert resp.status_code == 200

    def test_response_has_required_fields(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert "version" in body
        assert "uptime_seconds" in body
        assert "started_at" in body
        assert "python_version" in body
        assert "hostname" in body
        assert "environment" in body
        assert "git_sha" in body
        assert "git_branch" in body
        assert "git_commit_timestamp" in body
        assert "git_commit_message" in body
        assert "build_timestamp" in body
        assert "ci_job_url" in body

    def test_version_is_non_empty_string(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert isinstance(body["version"], str)
        assert body["version"]

    def test_uptime_is_positive_int(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert isinstance(body["uptime_seconds"], int)
        assert body["uptime_seconds"] >= 0

    def test_started_at_is_valid_iso_datetime(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        from datetime import datetime

        parsed = datetime.fromisoformat(body["started_at"])
        # started_at is UTC and tz-aware — it must round-trip to the same instant
        assert parsed.tzinfo is not None
        assert parsed.isoformat() == body["started_at"]

    def test_environment_defaults_to_development(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert body["environment"] == "development"

    def test_build_metadata_fields_are_strings(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        for field in (
            "git_sha",
            "git_branch",
            "git_commit_timestamp",
            "git_commit_message",
            "build_timestamp",
            "ci_job_url",
        ):
            assert isinstance(body[field], str), f"{field} should be a string"

    def test_build_metadata_falls_back_to_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert not body["git_sha"]
        assert not body["ci_job_url"]
