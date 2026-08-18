"""FAR-309 PR B break-glass invariant — per-scope (guardrail admin routes) +
org-global (guardrail kill-switch).

A break-glass account — live or denied — must never be able to:
  * read the ELEVATED guardrail config (``guardrail.manage``)
  * propose / apply / reject a guardrail config change
  * disable (or read) the org guardrail kill-switch

The ``deny_break_glass_mint`` DI marker on each route returns a uniform 403
BEFORE the handler runs. A normal (non-break-glass) admin is unaffected.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _breakglass_account() -> MagicMock:
    account = MagicMock()
    account.is_break_glass = True
    account.active = True
    account.break_glass_deactivated_at = None
    account.break_glass_expires_at = datetime.now(UTC) + timedelta(hours=1)
    return account


def _normal_account() -> MagicMock:
    account = MagicMock()
    account.is_break_glass = False
    account.active = True
    account.break_glass_deactivated_at = None
    account.break_glass_expires_at = None
    return account


def _make_session(account: MagicMock) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.get = AsyncMock(return_value=account)
    # Default usable execute result: no rows / no scalars — so the route
    # handler body (when reached by a normal account) resolves cleanly.
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    exec_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=exec_result)
    return session


def _breakglass_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="breakglass@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )


def _admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )


@pytest.fixture
def breakglass_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_current_user] = lambda: _breakglass_principal()
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="breakglass@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield _make_session(_breakglass_account())

    app.dependency_overrides[get_db_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def normal_admin_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_current_user] = lambda: _admin_principal()
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield _make_session(_normal_account())

    app.dependency_overrides[get_db_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Per-scope: guardrail admin routes (elevated read + propose/apply/reject)
# ---------------------------------------------------------------------------


def test_breakglass_denied_elevated_read(breakglass_client: TestClient) -> None:
    resp = breakglass_client.get("/api/v1/guardrails/config/elevated")
    assert resp.status_code == 403
    assert "Break-glass" in resp.json()["detail"]


def test_breakglass_denied_propose(breakglass_client: TestClient) -> None:
    resp = breakglass_client.post("/api/v1/guardrails/config/propose", json={"config_yaml": "version: 1\n"})
    assert resp.status_code == 403


def test_breakglass_denied_apply(breakglass_client: TestClient) -> None:
    resp = breakglass_client.post("/api/v1/guardrails/config/apply")
    assert resp.status_code == 403


def test_breakglass_denied_reject(breakglass_client: TestClient) -> None:
    resp = breakglass_client.post("/api/v1/guardrails/config/reject")
    assert resp.status_code == 403


def test_normal_admin_still_reads_elevated(normal_admin_client: TestClient) -> None:
    """A normal (non-break-glass) admin passes the deny marker — the deny is
    specific to break-glass accounts, not a blanket admin block."""
    resp = normal_admin_client.get("/api/v1/guardrails/config/elevated")
    # The deny marker passes; the handler then reaches the mocked DB (no pin /
    # no rows) and serves the empty masked-free config.
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Org-global: guardrail kill-switch
# ---------------------------------------------------------------------------


def test_breakglass_cannot_disable_kill_switch(breakglass_client: TestClient) -> None:
    resp = breakglass_client.put(f"/api/v1/admin/orgs/{_ORG_ID}/guardrails/kill-switch", json={"enabled": False})
    assert resp.status_code == 403


def test_breakglass_cannot_enable_kill_switch(breakglass_client: TestClient) -> None:
    resp = breakglass_client.put(f"/api/v1/admin/orgs/{_ORG_ID}/guardrails/kill-switch", json={"enabled": True})
    assert resp.status_code == 403


def test_breakglass_cannot_read_kill_switch(breakglass_client: TestClient) -> None:
    resp = breakglass_client.get(f"/api/v1/admin/orgs/{_ORG_ID}/guardrails/kill-switch")
    assert resp.status_code == 403


def test_normal_admin_read_kill_switch_passes_deny_marker(normal_admin_client: TestClient) -> None:
    resp = normal_admin_client.get(f"/api/v1/admin/orgs/{_ORG_ID}/guardrails/kill-switch")
    # The deny marker passes; the handler's org lookup on the mocked DB returns
    # no org -> 404 (proving the marker did not over-block).
    assert resp.status_code == 404
