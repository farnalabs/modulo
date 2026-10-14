"""Prove-the-fix tests for FAR-1625: a contended per-pipeline snapshot lock
surfaces as a retryable 503 on ``POST /api/v1/runs`` — never a generic 500.

The snapshot machinery raises ``SnapshotLockNotAvailableError`` when either
bounded wait on the snapshot path is exhausted: the graph-copy advisory lock's
acquisition budget, or the allocation row lock's transaction-scoped
``lock_timeout``. The route must map that to HTTP 503 (matching the webhook and
MCP trigger paths), so a lost race can be retried rather than reading as an
unexpected fault. This exercises the REAL request path (TestClient -> route ->
``except`` handler), so the wiring is covered — not just the handler body.

Removing the ``except SnapshotLockNotAvailableError`` arm would let the broad
``except Exception`` swallow it as a 500, and this test fails.
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
from modulo.core.exceptions import SnapshotLockNotAvailableError
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


def test_post_runs_maps_snapshot_lock_contention_to_retryable_503(client: TestClient) -> None:
    """FAR-1625: SnapshotLockNotAvailableError from snapshot creation maps to 503
    and states the contention, not a swallowed generic 500."""
    with (
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            new=AsyncMock(side_effect=SnapshotLockNotAvailableError("allocation lock busy")),
        ),
        patch("modulo.api.routes.runs.get_pipeline", new=AsyncMock(return_value=MagicMock(id=_PIPELINE_ID))),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_user_context", new=AsyncMock()),
        patch("modulo.db.settings_resolver.resolve_authz_enforce", new=AsyncMock(return_value=False)),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {}},
        )
    assert resp.status_code == 503
    assert "snapshot lock unavailable" in resp.text.lower()
