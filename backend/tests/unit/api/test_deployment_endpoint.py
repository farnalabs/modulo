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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.api.routes import deployment
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
    session = MagicMock()
    result = MagicMock()
    result.scalar_one.return_value = 0
    session.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[deployment.get_system_db_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDeploymentDrain:
    @pytest.mark.parametrize(
        ("authorization", "expected_status"),
        [(None, 503), ("Basic secret", 503), ("Bearer wrong", 403)],
    )
    def test_missing_or_invalid_token_is_rejected(
        self, client: TestClient, authorization: str | None, expected_status: int
    ) -> None:
        headers = {} if authorization is None else {"Authorization": authorization}
        with patch.dict("os.environ", {"MODULO_DEPLOY_DRAIN_TOKEN": "right"}, clear=False):
            response = client.post("/api/v1/deployment/drain?active=true", headers=headers)
        assert response.status_code == expected_status

    def test_unconfigured_token_is_rejected(self, client: TestClient) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = client.post("/api/v1/deployment/drain?active=true", headers={"Authorization": "Bearer right"})
        assert response.status_code == 503

    @pytest.mark.parametrize("active", [True, False])
    def test_valid_token_persists_value_and_audits(self, client: TestClient, active: bool) -> None:
        session = app.dependency_overrides[deployment.get_system_db_session]()
        with (
            patch.dict("os.environ", {"MODULO_DEPLOY_DRAIN_TOKEN": "right"}, clear=False),
            patch.object(deployment, "_set_drain", new_callable=AsyncMock) as set_drain,
        ):
            response = client.post(
                f"/api/v1/deployment/drain?active={str(active).lower()}",
                headers={"Authorization": "Bearer right"},
            )
        assert response.status_code == 200
        assert response.json() == {"active": active}
        set_drain.assert_awaited_once_with(session, active)

    @pytest.mark.asyncio
    async def test_set_drain_writes_config_and_audit(self) -> None:
        session = MagicMock()
        with (
            patch.object(deployment, "acquire_deployment_drain_lock", new_callable=AsyncMock) as lock,
            patch.object(deployment, "update_config", new_callable=AsyncMock) as update,
            patch.object(deployment, "append_system_audit_event", new_callable=AsyncMock) as audit,
        ):
            await deployment._set_drain(session, True)
        lock.assert_awaited_once_with(session)
        update.assert_awaited_once()
        assert update.await_args.args[:2] == (session, "deployment_drain")
        assert update.await_args.args[2]["active"] is True
        audit.assert_awaited_once_with(
            session,
            event_type="deployment_drain_changed",
            resource_type="deployment",
            payload_json={"active": True, "source": "deploy-workflow"},
        )


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
        assert body["active_sandbox_runs"] == 0

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

    def test_active_sandbox_runs_is_reported(self, client: TestClient) -> None:
        session = app.dependency_overrides[deployment.get_system_db_session]()
        result = MagicMock()
        result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=result)
        body = client.get("/api/v1/deployment").json()
        assert body["active_sandbox_runs"] == 3

    def test_active_sandbox_query_excludes_non_running_runs(self, client: TestClient) -> None:
        session = app.dependency_overrides[deployment.get_system_db_session]()
        result = MagicMock()
        result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=result)
        client.get("/api/v1/deployment")
        statements = [str(call.args[0]) for call in session.execute.call_args_list]
        assert any("runs.status" in sql for sql in statements)
        assert any("sandbox_dispatch_state" in sql for sql in statements)

    def test_build_metadata_falls_back_to_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/deployment")
        body = resp.json()
        assert not body["git_sha"]
