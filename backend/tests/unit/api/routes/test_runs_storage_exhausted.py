"""Prove-the-fix tests for FAR-426: StorageExhaustedError -> 503 storage_exhausted.

These exercise the REAL request path (FastAPI TestClient -> route -> Starlette
exception handler) so the wiring is covered, not just the handler body. The
handler-returns-503 unit test already existed; this catches the regression where
a broad ``except Exception`` in the route swallowed StorageExhaustedError before
Starlette could map it.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user_or_api_key
from modulo.auth.jwt import TenantPrincipal
from modulo.db.capacity import StorageExhaustedError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


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


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_post_runs_returns_503_storage_exhausted(client: TestClient) -> None:
    """StorageExhaustedError (fixed DB at hard-stop) must map to 503 + problem type.

    The capacity gate (``enforce_capacity_gate``) is stubbed to raise exactly as
    it would when ``db_capacity_status`` reports a fixed DB at/over the 98%
    hard-stop, so this exercises the real wiring: route -> gate -> Starlette
    handler -> 503 (not a swallowed 500).
    """
    fake_snapshot = MagicMock()
    fake_snapshot.id = _PIPELINE_ID
    fake_snapshot.graph_json = {"nodes": [], "edges": []}
    with (
        patch("modulo.db.capacity.enforce_capacity_gate", new=AsyncMock(side_effect=StorageExhaustedError("full"))),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            new=AsyncMock(return_value=fake_snapshot),
        ),
        patch("modulo.api.routes.runs.get_pipeline", new=AsyncMock(return_value=MagicMock(id=_PIPELINE_ID))),
        patch("modulo.api.routes.runs._validate_run_input_basics", new=AsyncMock()),
        patch("modulo.api.routes.runs._enforce_trigger_rate_limit", new=AsyncMock(return_value=None)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_user_context", new=AsyncMock()),
        patch("modulo.db.settings_resolver.resolve_authz_enforce", new=AsyncMock(return_value=False)),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {}},
        )
    assert resp.status_code == 503
    body = resp.json()
    assert "storage_exhausted" in body.get("type", "")
