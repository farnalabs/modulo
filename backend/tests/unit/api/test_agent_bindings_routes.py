"""Route-level coverage tests for the per-agent runner bindings endpoints (FAR-592 / D6).

Covers the list / replace / delete binding routes in ``modulo.api.routes.agents``:
happy paths, agent-not-found (404), org-mismatch (404), the replace IntegrityError
-> 409 mapping, and the list SQLAlchemyError -> 503 mapping.

Credentials/values are never asserted on the wire (they are not returned by the
response model) — only env-var names and binding ids.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_AGENT_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()
_BINDING_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PREFIX = "modulo.api.routes.agents."
# The route imports the binding CRUD functions by name, so they resolve from the
# route module namespace. Patch there (not the crud module) for the route to pick
# up the mocked implementations.
_CRUD = _PREFIX


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_agent() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    return a


def _make_binding() -> MagicMock:
    b = MagicMock()
    b.id = _BINDING_ID
    b.organisation_id = _ORG_ID
    b.agent_id = _AGENT_ID
    b.model_backend_id = _BACKEND_ID
    b.target_env_var = "OPENAI_API_KEY"
    b.source_field = "api_key"
    b.created_at = _NOW
    b.updated_at = _NOW
    return b


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=default_result)
    session.refresh = AsyncMock(return_value=None)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _rls_patches() -> list:
    return [
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ]


@contextmanager
def _patched_rls() -> Generator[None, None, None]:
    with ExitStack() as stack:
        for p in _rls_patches():
            stack.enter_context(p)
        yield


# ---------------------------------------------------------------------------
# GET /{agent_id}/bindings
# ---------------------------------------------------------------------------


def test_list_bindings_returns_items(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}list_bindings_for_agent", new=AsyncMock(return_value=[_make_binding()])),
        _patched_rls(),
    ):
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/bindings")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["target_env_var"] == "OPENAI_API_KEY"
    assert items[0]["source_field"] == "api_key"
    assert items[0]["agent_id"] == str(_AGENT_ID)


def test_list_bindings_empty(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}list_bindings_for_agent", new=AsyncMock(return_value=[])),
        _patched_rls(),
    ):
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/bindings")
    assert resp.status_code == 200, resp.text
    assert not resp.json()["items"]


def test_list_bindings_agent_not_found(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=None)),
        patch(f"{_CRUD}list_bindings_for_agent", new=AsyncMock(return_value=[])),
        _patched_rls(),
    ):
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/bindings")
    assert resp.status_code == 404, resp.text


def test_list_bindings_org_mismatch(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    other_org = _make_agent()
    other_org.organisation_id = uuid.uuid4()
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=other_org)),
        patch(f"{_CRUD}list_bindings_for_agent", new=AsyncMock(return_value=[])),
        _patched_rls(),
    ):
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/bindings")
    assert resp.status_code == 404, resp.text


def test_list_bindings_sqlalchemy_error_maps_503(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}list_bindings_for_agent", new=AsyncMock(side_effect=SQLAlchemyError("boom"))),
        _patched_rls(),
    ):
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/bindings")
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# PUT /{agent_id}/bindings
# ---------------------------------------------------------------------------


def test_replace_bindings_happy(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}replace_agent_bindings", new=AsyncMock(return_value=[_make_binding()])),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.put(
            f"/api/v1/agents/{_AGENT_ID}/bindings",
            json={
                "bindings": [
                    {
                        "model_backend_id": str(_BACKEND_ID),
                        "target_env_var": "OPENAI_API_KEY",
                        "source_field": "api_key",
                    }
                ]
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["target_env_var"] == "OPENAI_API_KEY"


def test_replace_bindings_agent_not_found(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=None)),
        patch(f"{_CRUD}replace_agent_bindings", new=AsyncMock(return_value=[])),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.put(
            f"/api/v1/agents/{_AGENT_ID}/bindings",
            json={
                "bindings": [
                    {
                        "model_backend_id": str(_BACKEND_ID),
                        "target_env_var": "OPENAI_API_KEY",
                        "source_field": "api_key",
                    }
                ]
            },
        )
    assert resp.status_code == 404, resp.text


def test_replace_bindings_integrity_maps_409(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}replace_agent_bindings", new=AsyncMock(side_effect=IntegrityError("s", {}, Exception()))),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.put(
            f"/api/v1/agents/{_AGENT_ID}/bindings",
            json={
                "bindings": [
                    {
                        "model_backend_id": str(_BACKEND_ID),
                        "target_env_var": "OPENAI_API_KEY",
                        "source_field": "api_key",
                    }
                ]
            },
        )
    assert resp.status_code == 409, resp.text


def test_replace_bindings_empty_set(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}replace_agent_bindings", new=AsyncMock(return_value=[])),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.put(f"/api/v1/agents/{_AGENT_ID}/bindings", json={"bindings": []})
    assert resp.status_code == 200, resp.text
    assert not resp.json()["items"]


# ---------------------------------------------------------------------------
# DELETE /{agent_id}/bindings/{binding_id}
# ---------------------------------------------------------------------------


def test_delete_binding_happy(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}delete_binding", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.delete(f"/api/v1/agents/{_AGENT_ID}/bindings/{_BINDING_ID}")
    assert resp.status_code == 204, resp.text


def test_delete_binding_not_found(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_CRUD}delete_binding", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.delete(f"/api/v1/agents/{_AGENT_ID}/bindings/{_BINDING_ID}")
    assert resp.status_code == 404, resp.text


def test_delete_binding_agent_not_found(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=None)),
        patch(f"{_CRUD}delete_binding", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
        _patched_rls(),
    ):
        resp = http.delete(f"/api/v1/agents/{_AGENT_ID}/bindings/{_BINDING_ID}")
    assert resp.status_code == 404, resp.text
