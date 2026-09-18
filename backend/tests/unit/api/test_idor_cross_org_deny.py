"""Prove-the-fix tests for the IDOR cross-org read deny path (security phase a).

These tests pin the security behaviour this PR adds: a request for a resource
owned by another organisation must return 404, never the resource. Each test
mocks the org-scoped lookup (get_agent / get_connector_instance /
get_model_backend) to return an object whose ``organisation_id`` differs from
the principal's org, then asserts 404. Without the ``organisation_id !=
principal.organisation_id`` guard these endpoints would return 200/500 instead,
so every test here fails if the deny path is removed.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import (
    _get_engine,
    deny_break_glass_mint,
    get_db_session,
    get_plan_context,
)
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_WRONG_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000dead")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_AGENT_ID = uuid.uuid4()
_CONNECTOR_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()


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
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = MagicMock()
    session.execute = AsyncMock(return_value=default_result)
    return session


@pytest.fixture
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[deny_break_glass_mint] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _wrong_org_agent() -> MagicMock:
    a = MagicMock()
    a.organisation_id = _WRONG_ORG_ID
    a.prompt_version_history = [
        {
            "version": "v1",
            "template": "x",
            "created_at": "",
            "notes": "",
            "optimized_from": None,
            "eval_result_ids": [],
        },
        {
            "version": "v2",
            "template": "y",
            "created_at": "",
            "notes": "",
            "optimized_from": "v1",
            "eval_result_ids": [],
        },
    ]
    a.prompt_template = "x"
    return a


_PROMPT_VERSION_ENTRY = {
    "version": "v1",
    "template": "x",
    "created_at": "",
    "notes": "",
    "optimized_from": None,
    "eval_result_ids": [],
}


def _wrong_org_connector() -> MagicMock:
    ci = MagicMock()
    ci.organisation_id = _WRONG_ORG_ID
    return ci


def _wrong_org_backend() -> MagicMock:
    mb = MagicMock()
    mb.organisation_id = _WRONG_ORG_ID
    return mb


# ---------------------------------------------------------------------------
# Agents — get_agent deny path (agents.py get_agent_endpoint / optimize_prompt /
# list_prompt_versions / get_prompt_version_endpoint / diff_prompt_versions)
# ---------------------------------------------------------------------------


class TestAgentCrossOrgDeny:
    def test_get_agent_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=_wrong_org_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/agents/{_AGENT_ID}")
        assert resp.status_code == 404

    def test_optimize_prompt_wrong_org_returns_404(self, client: TestClient) -> None:
        optimize_result = SimpleNamespace(suggested_prompt="p", rationale="r", analysis="a")
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=_wrong_org_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
            patch(
                "modulo.api.routes.agents.get_eval_results_with_defs",
                return_value=([MagicMock()], [MagicMock()]),
            ),
            patch(
                "modulo.api.routes.agents.create_secrets_backend",
                return_value=MagicMock(get_secret=MagicMock(return_value="{}")),
            ),
            patch(
                "modulo.core.model_backend_hub._build_backend",
                return_value=MagicMock(invoke=AsyncMock(return_value=MagicMock(content="ok"))),
            ),
            patch(
                "modulo.api.routes.agents.PromptOptimizer",
                return_value=MagicMock(optimize=AsyncMock(return_value=optimize_result)),
            ),
        ):
            resp = client.post(
                f"/api/v1/agents/{_AGENT_ID}/prompts/v1/optimize",
                json={"eval_result_ids": [str(uuid.uuid4())]},
            )
        assert resp.status_code == 404

    def test_list_prompt_versions_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=_wrong_org_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/agents/{_AGENT_ID}/prompts")
        assert resp.status_code == 404

    def test_get_prompt_version_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=_wrong_org_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
            patch(
                "modulo.api.routes.agents.get_prompt_version",
                return_value=_PROMPT_VERSION_ENTRY,
            ),
        ):
            resp = client.get(f"/api/v1/agents/{_AGENT_ID}/prompts/v1")
        assert resp.status_code == 404

    def test_diff_prompt_versions_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=_wrong_org_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
            patch(
                "modulo.api.routes.agents.get_prompt_version",
                return_value=_PROMPT_VERSION_ENTRY,
            ),
        ):
            resp = client.post(
                f"/api/v1/agents/{_AGENT_ID}/prompts/diff",
                json={"version_a": "v1", "version_b": "v2"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Connectors — get_connector_instance deny path (connectors.py
# get_connector_endpoint / connector_health_endpoint / update_connector_endpoint)
# ---------------------------------------------------------------------------


class TestConnectorCrossOrgDeny:
    def test_get_connector_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.connectors.get_connector_instance", return_value=_wrong_org_connector()),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
        assert resp.status_code == 404

    def test_connector_health_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.connectors.get_connector_instance", return_value=_wrong_org_connector()),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}/health")
        assert resp.status_code == 404

    def test_update_connector_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.connectors.get_connector_instance", return_value=_wrong_org_connector()),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.patch(
                f"/api/v1/connectors/{_CONNECTOR_ID}",
                json={"name": "hijacked"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Model backends — get_model_backend deny path (model_backends.py
# get_model_backend_endpoint / list_pipeline_references_endpoint)
# ---------------------------------------------------------------------------


class TestModelBackendCrossOrgDeny:
    def test_get_model_backend_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.model_backends.get_model_backend", return_value=_wrong_org_backend()),
            patch("modulo.api.routes.model_backends.set_rls_org"),
            patch("modulo.api.routes.model_backends.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
        assert resp.status_code == 404

    def test_list_pipeline_references_wrong_org_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.model_backends.get_model_backend", return_value=_wrong_org_backend()),
            patch("modulo.api.routes.model_backends.set_rls_org"),
            patch("modulo.api.routes.model_backends.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}/pipeline-references")
        assert resp.status_code == 404
