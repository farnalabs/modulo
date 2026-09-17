"""Unit tests for team-scope enforcement on eval dataset and eval suite routes.

FAR-947: team-private eval datasets and eval suites (visibility='team') require
membership in the owning team.  Org-visible resources remain open to any org
member with the appropriate permission floor.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import (
    _get_engine,
    get_db_session,
    get_plan_context,
)
from modulo.api.main import app
from modulo.auth.dependencies import (
    get_current_tenant_user,
    get_current_tenant_user_or_api_key,
    get_current_user,
)
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_DATASET_ID = uuid.uuid4()
_SUITE_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_dataset(**overrides: Any) -> MagicMock:
    d = MagicMock()
    d.id = overrides.get("id", _DATASET_ID)
    d.organisation_id = _ORG_ID
    d.name = overrides.get("name", "Test Dataset")
    d.visibility = overrides.get("visibility", "team")
    d.owner_team_id = overrides.get("owner_team_id", _TEAM_ID)
    d.version = 1
    d.deleted_at = None
    d.created_at = _NOW
    d.updated_at = _NOW
    return d


def _make_suite(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = overrides.get("id", _SUITE_ID)
    s.organisation_id = _ORG_ID
    s.name = overrides.get("name", "Test Suite")
    s.visibility = overrides.get("visibility", "team")
    s.owner_team_id = overrides.get("owner_team_id", _TEAM_ID)
    s.version = 1
    s.description = None
    s.eval_definition_ids = []
    s.deleted_at = None
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_mock_session(
    *,
    resource_visibility: str = "team",
    owner_team_id: uuid.UUID | None = _TEAM_ID,
    is_member: bool = True,
    resource_type: str = "eval_dataset",
) -> AsyncMock:
    """Build a mock session that handles team gate + route queries."""
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _execute(stmt: Any, **kwargs: Any) -> MagicMock:
        stmt_str = str(stmt).lower()
        result = MagicMock()

        if "team_memberships" in stmt_str:
            row = MagicMock() if is_member else None
            result.first.return_value = row
        elif "eval_datasets" in stmt_str and resource_type == "eval_dataset":
            if resource_visibility is None and owner_team_id is None:
                result.first.return_value = None
                result.scalar_one_or_none.return_value = None
            else:
                result.first.return_value = (owner_team_id, resource_visibility)
                mock_obj = _make_dataset(
                    visibility=resource_visibility,
                    owner_team_id=owner_team_id,
                )
                result.scalar_one_or_none.return_value = mock_obj
        elif "eval_suites" in stmt_str and resource_type == "eval_suite":
            if resource_visibility is None and owner_team_id is None:
                result.first.return_value = None
                result.scalar_one_or_none.return_value = None
            else:
                result.first.return_value = (owner_team_id, resource_visibility)
                mock_obj = _make_suite(
                    visibility=resource_visibility,
                    owner_team_id=owner_team_id,
                )
                result.scalar_one_or_none.return_value = mock_obj
        elif "set_config" in stmt_str:
            result.scalar.return_value = None
        elif "authz_enforce" in stmt_str:
            result.scalar_one_or_none.return_value = None
        else:
            result.first.return_value = None
            result.all.return_value = []
            result.scalar_one_or_none.return_value = None

        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_runner_principal(
    *,
    org_role: str = "runner",
    team_id: uuid.UUID | None = None,
) -> TenantPrincipal:
    return TenantPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
        team_id=team_id,
    )


def _make_admin_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _setup_app_overrides(
    mock_session: AsyncMock,
    principal: TenantPrincipal,
    *,
    username: str = "runner",
    org_role: str = "runner",
) -> None:
    """Set up all required dependency overrides for the test client."""

    async def _override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def _override_tenant() -> TenantPrincipal:
        return principal

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=username,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = _override_tenant
    app.dependency_overrides[get_current_tenant_user_or_api_key] = _override_tenant

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


class TestEvalDatasetTeamScope:
    """Team-scope gate on eval dataset routes (FAR-947)."""

    def test_member_can_get_team_private_dataset(self) -> None:
        """A team member may GET a team-private eval dataset."""
        mock_session = _make_mock_session(resource_type="eval_dataset", is_member=True)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403_on_get(self) -> None:
        """A non-member gets 403 when trying to GET a team-private dataset."""
        mock_session = _make_mock_session(resource_type="eval_dataset", is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_org_admin_bypasses_team_gate_on_get(self) -> None:
        """An org-admin bypasses the team gate on GET."""
        mock_session = _make_mock_session(resource_type="eval_dataset", is_member=False)
        principal = _make_admin_principal()
        _setup_app_overrides(mock_session, principal, username="admin", org_role="admin")

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_org_visible_dataset_allowed_for_non_member(self) -> None:
        """An org-visible dataset is not team-gated."""
        mock_session = _make_mock_session(
            resource_type="eval_dataset",
            resource_visibility="org",
            owner_team_id=None,
            is_member=False,
        )
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403_on_patch(self) -> None:
        """A non-member gets 403 when trying to PATCH a team-private dataset."""
        mock_session = _make_mock_session(resource_type="eval_dataset", is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.patch(
                f"/api/v1/eval-datasets/{_DATASET_ID}",
                json={"name": "Updated Name"},
            )
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403_on_delete(self) -> None:
        """A non-member gets 403 when trying to DELETE a team-private dataset."""
        mock_session = _make_mock_session(resource_type="eval_dataset", is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.delete(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()


class TestEvalSuiteTeamScope:
    """Team-scope gate on eval suite routes (FAR-947)."""

    def test_member_can_get_team_private_suite(self) -> None:
        """A team member may GET a team-private eval suite."""
        mock_session = _make_mock_session(resource_type="eval_suite", is_member=True)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403_on_get(self) -> None:
        """A non-member gets 403 when trying to GET a team-private suite."""
        mock_session = _make_mock_session(resource_type="eval_suite", is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_org_admin_bypasses_team_gate_on_get(self) -> None:
        """An org-admin bypasses the team gate on GET."""
        mock_session = _make_mock_session(resource_type="eval_suite", is_member=False)
        principal = _make_admin_principal()
        _setup_app_overrides(mock_session, principal, username="admin", org_role="admin")

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_org_visible_suite_allowed_for_non_member(self) -> None:
        """An org-visible suite is not team-gated."""
        mock_session = _make_mock_session(
            resource_type="eval_suite",
            resource_visibility="org",
            owner_team_id=None,
            is_member=False,
        )
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.get(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403_on_patch(self) -> None:
        """A non-member gets 403 when trying to PATCH a team-private suite."""
        mock_session = _make_mock_session(resource_type="eval_suite", is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.patch(
                f"/api/v1/eval-suites/{_SUITE_ID}",
                json={"name": "Updated Suite"},
            )
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403_on_delete(self) -> None:
        """A non-member gets 403 when trying to DELETE a team-private suite."""
        mock_session = _make_mock_session(resource_type="eval_suite", is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.delete(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()
