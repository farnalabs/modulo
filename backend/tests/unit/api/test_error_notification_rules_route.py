"""Route-level tests for HTTP status-code mappings on error-notification rules.

Migration 0117 (#1376) added the DB-level unique backstop for the per-org
notification-rule cap. The create route must translate the resulting
IntegrityError into 422 (not 409 or a 500), so a duplicate default-seed or a
concurrent over-cap insert reads as a validation conflict to the caller. This
module pins that mapping at the route boundary.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _make_session(*, flush_raises: bool) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session.execute = AsyncMock(return_value=count_result)

    if flush_raises:
        session.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("UNIQUE constraint failed")))
    else:
        session.flush = AsyncMock(return_value=None)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _make_session(flush_raises=True)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    class _MockFactory:
        def __init__(self, s: AsyncMock) -> None:
            self._session = s

        def __call__(self) -> "_MockFactory":
            return self

        async def __aenter__(self) -> AsyncMock:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            pass

    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)
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

    class _AllFeatures:
        def feature_enabled(self, name: str) -> bool:
            return True

        def list_enabled_features(self) -> list:
            return []

        def tier(self) -> str:
            return "team"

        def has_license_key(self) -> bool:
            return True

    app.dependency_overrides[get_plan_context] = lambda: _AllFeatures()

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_rule_maps_unique_violation_to_422(client: TestClient) -> None:
    """IntegrityError (migration 0117 unique backstop) must surface as 422."""
    resp = client.post(
        "/api/v1/errors/notification-rules",
        json={"name": "dup-rule"},
    )

    assert resp.status_code == 422


class _CommunityPlan:
    """Plan-context stub with every feature disabled (community / no license)."""

    def feature_enabled(self, name: str) -> bool:
        return False

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "community"

    def has_license_key(self) -> bool:
        return False


@pytest.fixture
def community_client() -> Generator[TestClient, None, None]:
    session = _make_session(flush_raises=False)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    class _MockFactory:
        def __init__(self, s: AsyncMock) -> None:
            self._session = s

        def __call__(self) -> "_MockFactory":
            return self

        async def __aenter__(self) -> AsyncMock:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            pass

    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)
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
    app.dependency_overrides[get_plan_context] = lambda: _CommunityPlan()

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_community_plan_webhook_rule_returns_402(community_client: TestClient) -> None:
    """Community tier has no webhook action — a webhook rule must be 402."""
    resp = community_client.post(
        "/api/v1/errors/notification-rules",
        json={"name": "webhook-rule", "action_type": "webhook", "webhook_url": "https://example.com/hook"},
    )

    assert resp.status_code == 402
    assert "Team tier" in resp.text


def test_community_plan_in_app_rule_passes_tier_check(community_client: TestClient) -> None:
    """Community tier may create in_app rules (3 max) — the tier check must pass."""
    resp = community_client.post(
        "/api/v1/errors/notification-rules",
        json={"name": "in-app-rule", "action_type": "in_app"},
    )

    assert resp.status_code != 402
