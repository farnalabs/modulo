"""402-sweep test for team-tier feature gates.

Iterates a curated table of (feature, method, path) tuples covering the
team-gated routes and asserts:
  - with a community plan (feature disabled) the route returns 402;
  - with a team plan (feature enabled) the route does NOT return 402 (the gate
    passed; the route may 404/422/500 on missing data — we only care the gate
    didn't block).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_DUMMY_UUID = uuid.UUID("00000000-0000-0000-0000-000000000005")


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


class _TeamPlan:
    """Plan-context stub with every feature enabled (paid team license)."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar=MagicMock(return_value=0),
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
            first=MagicMock(return_value=None),
        )
    )
    return session


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _build_client(plan: object) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_plan_context] = lambda: plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def community_client() -> Generator[TestClient, None, None]:
    yield from _build_client(_CommunityPlan())


@pytest.fixture
def team_client() -> Generator[TestClient, None, None]:
    yield from _build_client(_TeamPlan())


# (feature_name, method, path)
# One representative route per team feature that carries a require_feature gate.
TEAM_ROUTES: list[tuple[str, str, str]] = [
    ("sso", "get", "/api/v1/admin/sso/providers"),
    ("team_rbac", "get", "/api/v1/teams"),
    ("audit_viewer", "get", "/api/v1/admin/audit/export"),
    ("admin_spend_limits", "get", "/api/v1/admin/costs/limits"),
    ("admin_cost_controls", "get", "/api/v1/admin/costs/controls"),
    ("admin_cost_breakdown", "get", "/api/v1/admin/costs/export"),
    ("view_modes", "get", "/api/v1/views"),
    ("environment_profiles", "get", "/api/v1/environments"),
    ("plugin_management", "get", "/api/v1/plugins"),
    ("admin_run_retention", "get", "/api/v1/admin/runs/retention"),
    ("error_forwarders", "get", "/api/v1/errors/forwarders"),
    ("schema_version_history", "get", f"/api/v1/schemas/{_SCHEMA_ID}/versions"),
    ("runtime_config", "get", "/api/v1/admin/runtime-config"),
    ("rate_limits", "get", "/api/v1/admin/rate-limits"),
    ("observability", "get", "/api/v1/settings/observability"),
    ("analytics_page", "get", "/api/v1/analytics/query"),
    ("error_tracking", "get", "/api/v1/errors"),
    ("email_config", "get", f"/api/v1/admin/org/{_DUMMY_UUID}/email-settings"),
]


def _call(client: TestClient, method: str, path: str):
    return getattr(client, method)(path)


@pytest.mark.parametrize("feature,method,path", TEAM_ROUTES)
def test_community_plan_returns_402(community_client: TestClient, feature: str, method: str, path: str) -> None:
    resp = _call(community_client, method, path)
    assert resp.status_code == 402, (
        f"Expected 402 for {method.upper()} {path} (feature '{feature}' disabled) but got {resp.status_code}: "
        f"{resp.text[:200]}"
    )
    assert feature in resp.text


@pytest.mark.parametrize("feature,method,path", TEAM_ROUTES)
def test_team_plan_does_not_402(team_client: TestClient, feature: str, method: str, path: str) -> None:
    resp = _call(team_client, method, path)
    assert resp.status_code != 402, (
        f"Feature '{feature}' gate blocked {method.upper()} {path} even though the team plan enables it"
    )
