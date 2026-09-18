"""Route-level tests for per-agent Model Backend runner bindings (FAR-592 / D6).

Covers the three HTTP endpoints in ``modulo.api.routes.agents``:

* ``GET    /api/v1/agents/{agent_id}/bindings``
* ``PUT    /api/v1/agents/{agent_id}/bindings``
* ``DELETE /api/v1/agents/{agent_id}/bindings/{binding_id}``

The route layer previously surfaced ``BindingValidationError`` (a ``ValueError``
from save-time validation) as HTTP 500 instead of 400 — these tests pin the
correct mapping and the audit-on-delete behaviour. CRUD is mocked; this is the
HTTP contract, not the persistence layer (covered by the integration suite).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.runner_binding_constraints import BindingValidationError
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_BACKEND_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_BINDING_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_SECRET_KEY = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_SECRET_KEY,
        fernet_key=_SECRET_KEY,
        modulo_admin_password="testpass",
    )


class _Binding:
    """Minimal stand-in for an ``AgentRunnerBinding`` row (response mapping)."""

    def __init__(self, target_env_var: str = "OPENCODE_API_KEY", source_field: str = "api_key") -> None:
        self.id = _BINDING_ID
        self.organisation_id = _ORG_ID
        self.agent_id = _AGENT_ID
        self.model_backend_id = _BACKEND_ID
        self.target_env_var = target_env_var
        self.source_field = source_field
        self.created_at = _NOW
        self.updated_at = _NOW


def _make_agent() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    return a


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    _exec_result = MagicMock()
    _exec_result.scalars.return_value = []
    session.execute = AsyncMock(return_value=_exec_result)
    return session


_AGENTS_PREFIX = "modulo.api.routes.agents."


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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _patch_core(agent: MagicMock | None = None) -> list[Any]:
    patchers = [
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=agent if agent is not None else _make_agent()),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}list_bindings_for_agent", new=AsyncMock(return_value=[])),
        patch(f"{_AGENTS_PREFIX}append_audit_event", new=AsyncMock()),
    ]
    for p in patchers:
        p.start()
    return patchers


def _stop(patchers: list[Any]) -> None:
    for p in patchers:
        p.stop()


def test_list_bindings_round_trip(client: TestClient) -> None:
    with (
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=_make_agent()),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}list_bindings_for_agent", new=AsyncMock(return_value=[_Binding()])),
    ):
        resp = client.get(f"/api/v1/agents/{_AGENT_ID}/bindings")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["target_env_var"] == "OPENCODE_API_KEY"
    assert items[0]["source_field"] == "api_key"


def test_list_bindings_unknown_agent_404(client: TestClient) -> None:
    with (
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=None),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}list_bindings_for_agent", new=AsyncMock(return_value=[])),
    ):
        resp = client.get(f"/api/v1/agents/{uuid.uuid4()}/bindings")
    assert resp.status_code == 404


def test_replace_bindings_round_trip(client: TestClient) -> None:
    with (
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=_make_agent()),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}list_bindings_for_agent", new=AsyncMock(return_value=[])),
        patch(f"{_AGENTS_PREFIX}replace_agent_bindings", new=AsyncMock(return_value=[_Binding()])),
        patch(f"{_AGENTS_PREFIX}append_audit_event", new=AsyncMock()),
    ):
        resp = client.put(
            f"/api/v1/agents/{_AGENT_ID}/bindings",
            json={
                "bindings": [
                    {
                        "model_backend_id": str(_BACKEND_ID),
                        "target_env_var": "OPENCODE_API_KEY",
                        "source_field": "api_key",
                    }
                ]
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["target_env_var"] == "OPENCODE_API_KEY"


def test_replace_bindings_reserved_env_var_returns_400(client: TestClient) -> None:
    """A reserved/malformed ``target_env_var`` must be HTTP 400, not 500."""
    patchers = _patch_core()
    try:
        with patch(
            f"{_AGENTS_PREFIX}replace_agent_bindings",
            new=AsyncMock(side_effect=BindingValidationError("reserved env var: PATH")),
        ):
            resp = client.put(
                f"/api/v1/agents/{_AGENT_ID}/bindings",
                json={
                    "bindings": [
                        {"model_backend_id": str(_BACKEND_ID), "target_env_var": "PATH", "source_field": "api_key"}
                    ]
                },
            )
    finally:
        _stop(patchers)
    assert resp.status_code == 400, resp.text


def test_replace_bindings_unknown_source_field_returns_400(client: TestClient) -> None:
    """An unknown ``source_field`` must be HTTP 400, not 500."""
    patchers = _patch_core()
    try:
        with patch(
            f"{_AGENTS_PREFIX}replace_agent_bindings",
            new=AsyncMock(side_effect=BindingValidationError("unknown source_field: root_password")),
        ):
            resp = client.put(
                f"/api/v1/agents/{_AGENT_ID}/bindings",
                json={
                    "bindings": [
                        {
                            "model_backend_id": str(_BACKEND_ID),
                            "target_env_var": "MY_KEY",
                            "source_field": "root_password",
                        }
                    ]
                },
            )
    finally:
        _stop(patchers)
    assert resp.status_code == 400, resp.text


def test_replace_bindings_unknown_agent_404(client: TestClient) -> None:
    with (
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=None),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}replace_agent_bindings", new=AsyncMock(return_value=[_Binding()])),
    ):
        resp = client.put(
            f"/api/v1/agents/{uuid.uuid4()}/bindings",
            json={
                "bindings": [
                    {
                        "model_backend_id": str(_BACKEND_ID),
                        "target_env_var": "OPENCODE_API_KEY",
                        "source_field": "api_key",
                    }
                ]
            },
        )
    assert resp.status_code == 404


def test_delete_binding_audits_on_success(client: TestClient) -> None:
    mock_audit = AsyncMock()
    with (
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=_make_agent()),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}delete_binding", new=AsyncMock(return_value=True)),
        patch(f"{_AGENTS_PREFIX}append_audit_event", new=mock_audit),
    ):
        resp = client.delete(f"/api/v1/agents/{_AGENT_ID}/bindings/{_BINDING_ID}")
    assert resp.status_code == 204
    mock_audit.assert_awaited_once()


def test_delete_binding_not_found_404_and_no_audit(client: TestClient) -> None:
    mock_audit = AsyncMock()
    with (
        patch(f"{_AGENTS_PREFIX}get_agent", return_value=_make_agent()),
        patch(f"{_AGENTS_PREFIX}set_rls_org"),
        patch(f"{_AGENTS_PREFIX}delete_binding", new=AsyncMock(return_value=False)),
        patch(f"{_AGENTS_PREFIX}append_audit_event", new=mock_audit),
    ):
        resp = client.delete(f"/api/v1/agents/{_AGENT_ID}/bindings/{uuid.uuid4()}")
    assert resp.status_code == 404
    mock_audit.assert_not_awaited()
