"""Unit tests for the deployment info endpoint."""

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.settings import Settings, get_settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="",
    )


@pytest.fixture()
def client() -> TestClient:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDeploymentInfo:
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

    def test_version_is_non_empty_string(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert isinstance(body["version"], str)
        assert len(body["version"]) > 0

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
        assert parsed is not None

    def test_environment_defaults_to_development(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert body["environment"] == "development"
