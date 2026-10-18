"""Unit tests for the deployment info endpoint.

FAR-880 follow-up: the endpoint stays UNAUTHENTICATED — the CI/CD deploy
pipelines (deploy.yml, rc-validate.yml, deploy-watchdog.yml,
deploy-staleness-check.yml) call it without a principal to verify which build
is live. Sensitive local fields (hostname, ci_job_url) are removed from the
response instead.

FAR-1975: ``git_branch`` and ``git_commit_message`` are also removed. They are
free text from the *deployment's* source repository (which a self-hosted
operator may keep private, so the "the repo is public" premise does not
generalise) and no deploy workflow reads them.
"""

from collections.abc import Generator
from datetime import datetime

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


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    # Deliberately NO auth principal override: the endpoint is public.
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDeploymentInfo:
    def test_unauthenticated_reachable_returns_200(self, client: TestClient) -> None:
        """No Authorization header — an unauthenticated caller still gets 200."""
        resp = client.get("/api/v1/deployment")
        assert resp.status_code == 200

    def test_response_has_required_fields(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert "version" in body
        assert "uptime_seconds" in body
        assert "started_at" in body
        assert "python_version" in body
        assert "environment" in body
        assert "git_sha" in body
        assert "git_commit_timestamp" in body
        assert "build_timestamp" in body

    def test_source_repo_free_text_absent(self, client: TestClient) -> None:
        """FAR-1975: no free text from the deployment's source repo is exposed."""
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert "git_branch" not in body
        assert "git_commit_message" not in body

    def test_sensitive_fields_absent(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert "hostname" not in body
        assert "ci_job_url" not in body

    def test_no_tenant_or_credential_fields(self, client: TestClient) -> None:
        """The unauthenticated response carries no org/tenant/user/credential key."""
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        forbidden_terms = ("org", "tenant", "user", "secret", "token", "password", "credential", "key")
        matched = [key for key in body if any(term in key.lower() for term in forbidden_terms)]
        assert not matched

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
            "git_commit_timestamp",
            "build_timestamp",
        ):
            assert isinstance(body[field], str), f"{field} should be a string"

    def test_build_metadata_falls_back_to_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert not body["git_sha"]
