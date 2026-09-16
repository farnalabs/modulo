"""Unit tests for org scoping on GET /runs/{id}/workspace-events (FAR-897 / #92).

The endpoint must load the run through the org-scoped CRUD helper (foreign-org
run ids answer 404, not a 200 with an empty timeline) and must carry an
explicit ``AuditEvent.organisation_id`` predicate instead of relying on RLS
alone.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FOREIGN_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",  # nosec - test-only value
    )


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: MagicMock()
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


def test_workspace_events_404_on_foreign_org_run(client: TestClient, mock_session: AsyncMock) -> None:
    """A run id belonging to another organisation answers 404, not 200 []."""
    with patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock) as mock_get_run:
        mock_get_run.return_value = None
        response = client.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")

    assert response.status_code == 404
    body: dict[str, Any] = response.json()
    assert body["detail"] == "Run not found"
    # The ownership check is keyed to the caller's organisation.
    assert mock_get_run.call_args.kwargs.get("organisation_id") == _ORG_ID
    # Without the run, no audit-event query is executed (only set_rls_org's
    # internal statement runs).
    executed_sql = [str(call.args[0]) for call in mock_session.execute.await_args_list]
    assert not any("audit_events" in sql for sql in executed_sql)


def test_workspace_events_returns_events_for_owned_run_after_org_check(
    client: TestClient, mock_session: AsyncMock
) -> None:
    """A valid (same-org) run returns its own events timeline."""
    result = MagicMock()
    event = MagicMock()
    event.event_type = "workspace_started"
    event.payload_json = {"detail": "workspace prepared"}
    event.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    result.scalars.return_value.all.return_value = [event]
    mock_session.execute = AsyncMock(return_value=result)

    with patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock) as mock_get_run:
        mock_get_run.return_value = MagicMock()
        response = client.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")

    assert response.status_code == 200
    assert mock_get_run.call_args.kwargs.get("organisation_id") == _ORG_ID
    body = response.json()
    assert body  # the timeline is non-empty
    assert body[0]["event"] == "started"
    assert body[0]["detail"] == "workspace prepared"


def test_workspace_events_audit_query_carries_org_predicate(client: TestClient, mock_session: AsyncMock) -> None:
    """The audit-event select is pinned to the caller's organisation_id."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=result)

    with patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock):
        response = client.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")

    assert response.status_code == 200
    executed_stmts = [call.args[0] for call in mock_session.execute.await_args_list]
    audit_stmts = [s for s in executed_stmts if "audit_events" in str(s.compile())]
    assert audit_stmts  # the audit-event select ran exactly once
    assert len(audit_stmts) == 1
    sql = str(audit_stmts[0].compile())
    assert "audit_events.organisation_id" in sql
