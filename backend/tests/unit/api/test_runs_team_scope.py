"""Unit tests for team-scope enforcement on POST /api/v1/runs (trigger_run).

FAR-946: team-private pipelines (visibility='team') require membership in the
owning team.  Org-visible pipelines remain open to any org member with the
``run.trigger`` role floor.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
_PIPELINE_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_THREAD_ID = str(uuid.uuid4())
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline(**overrides: Any) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PIPELINE_ID)
    p.organisation_id = _ORG_ID
    p.name = overrides.get("name", "Team Pipeline")
    p.description = None
    p.visibility = overrides.get("visibility", "team")
    p.owner_team_id = overrides.get("owner_team_id", _TEAM_ID)
    p.folder_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.created_by = uuid.uuid4()
    p.account_id = p.created_by
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_run() -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.pipeline_id = _PIPELINE_ID
    r.pipeline_name = "Team Pipeline"
    r.pipeline = None
    r.status = "pending"
    r.langgraph_thread_id = _THREAD_ID
    r.error_detail = None
    r.error_code = None
    r.total_cost_usd = None
    r.total_tokens = None
    r.node_token_usage = None
    r.cost_breakdown = None
    r.trigger_type = "manual"
    r.trigger_id = None
    r.account_id = None
    r.heartbeat_at = None
    r.work_item_refs = None
    r.parent_run_id = None
    r.snapshot_id = None
    r.input_payload = None
    return r


def _make_snapshot() -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.graph_json = {"nodes": [{"id": "node-a", "role": None}], "edges": []}
    return s


def _make_mock_session(
    *,
    pipeline_visibility: str = "team",
    owner_team_id: uuid.UUID | None = _TEAM_ID,
    is_member: bool = True,
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
        elif "pipelines" in stmt_str:
            if pipeline_visibility is None and owner_team_id is None:
                result.first.return_value = None
                result.scalar_one_or_none.return_value = None
            else:
                # Return a Row-like tuple: (owner_team_id, visibility)
                result.first.return_value = (owner_team_id, pipeline_visibility)
                result.scalar_one_or_none.return_value = MagicMock(
                    visibility=pipeline_visibility,
                    owner_team_id=owner_team_id,
                    deleted_at=None,
                )
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


class TestTriggerRunTeamScope:
    """Team-scope gate on POST /api/v1/runs (FAR-946)."""

    def test_member_can_trigger_team_private_pipeline(self) -> None:
        """A runner who is a member of the owning team may trigger."""
        pipeline = _make_pipeline()
        run = _make_run()
        mock_session = _make_mock_session(is_member=True)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            with (
                patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
                patch("modulo.api.routes.runs.create_snapshot_from_live_graph", return_value=_make_snapshot()),
                patch("modulo.api.routes.runs.create_run", return_value=run),
                patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
                patch("modulo.api.routes.runs.set_rls_org"),
            ):
                resp = client.post(
                    "/api/v1/runs",
                    json={"pipeline_id": str(_PIPELINE_ID)},
                )

            assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
            assert resp.json()["run_id"] == str(_RUN_ID)
        finally:
            app.dependency_overrides.clear()

    def test_non_member_denied_403(self) -> None:
        """A runner who is NOT a member of the owning team gets 403."""
        mock_session = _make_mock_session(is_member=False)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.post(
                "/api/v1/runs",
                json={"pipeline_id": str(_PIPELINE_ID)},
            )

            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_org_admin_bypasses_team_gate(self) -> None:
        """An org-admin bypasses the team gate entirely."""
        pipeline = _make_pipeline()
        run = _make_run()
        mock_session = _make_mock_session(is_member=False)
        principal = _make_admin_principal()
        _setup_app_overrides(
            mock_session,
            principal,
            username="admin",
            org_role="admin",
        )

        try:
            client = TestClient(app)
            with (
                patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
                patch("modulo.api.routes.runs.create_snapshot_from_live_graph", return_value=_make_snapshot()),
                patch("modulo.api.routes.runs.create_run", return_value=run),
                patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
                patch("modulo.api.routes.runs.set_rls_org"),
            ):
                resp = client.post(
                    "/api/v1/runs",
                    json={"pipeline_id": str(_PIPELINE_ID)},
                )

            assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_org_visible_pipeline_allowed_for_any_member(self) -> None:
        """An org-visible pipeline is not team-gated — any runner may trigger."""
        pipeline = _make_pipeline(visibility="org", owner_team_id=None)
        run = _make_run()
        mock_session = _make_mock_session(
            pipeline_visibility="org",
            owner_team_id=None,
            is_member=False,
        )
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            with (
                patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
                patch("modulo.api.routes.runs.create_snapshot_from_live_graph", return_value=_make_snapshot()),
                patch("modulo.api.routes.runs.create_run", return_value=run),
                patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
                patch("modulo.api.routes.runs.set_rls_org"),
            ):
                resp = client.post(
                    "/api/v1/runs",
                    json={"pipeline_id": str(_PIPELINE_ID)},
                )

            assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()

    def test_pipeline_not_found_returns_404(self) -> None:
        """A missing pipeline returns 404 from the team gate."""
        mock_session = _make_mock_session(pipeline_visibility=None, owner_team_id=None)
        principal = _make_runner_principal()
        _setup_app_overrides(mock_session, principal)

        try:
            client = TestClient(app)
            resp = client.post(
                "/api/v1/runs",
                json={"pipeline_id": str(uuid.uuid4())},
            )

            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        finally:
            app.dependency_overrides.clear()


class TestTriggerRunTeamScopeResolver:
    """Unit tests for resolve_trigger_run_team_scope (body-based resolver)."""

    @pytest.mark.asyncio
    async def test_resolver_reads_pipeline_id_from_body(self) -> None:
        """Resolver extracts pipeline_id from the JSON request body."""
        from modulo.api.team_scope import resolve_trigger_run_team_scope

        pid = uuid.uuid4()

        mock_session = AsyncMock()
        result_mock = MagicMock()
        # Return a Row-like tuple: (owner_team_id, visibility)
        result_mock.first.return_value = (_TEAM_ID, "team")
        mock_session.execute = AsyncMock(return_value=result_mock)

        request = MagicMock()
        request.body = AsyncMock(return_value=f'{{"pipeline_id": "{pid}"}}'.encode())

        resource = await resolve_trigger_run_team_scope(request, mock_session)
        assert resource is not None
        assert resource.owner_team_id == _TEAM_ID
        assert resource.visibility == "team"

    @pytest.mark.asyncio
    async def test_resolver_returns_none_for_missing_pipeline(self) -> None:
        """Resolver returns None when pipeline_id doesn't match any row."""
        from modulo.api.team_scope import resolve_trigger_run_team_scope

        pid = uuid.uuid4()
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.first.return_value = None
        mock_session.execute = AsyncMock(return_value=result_mock)

        request = MagicMock()
        request.body = AsyncMock(return_value=f'{{"pipeline_id": "{pid}"}}'.encode())

        resource = await resolve_trigger_run_team_scope(request, mock_session)
        assert resource is None

    @pytest.mark.asyncio
    async def test_resolver_returns_none_for_empty_body(self) -> None:
        """Resolver returns None when the body has no pipeline_id."""
        from modulo.api.team_scope import resolve_trigger_run_team_scope

        mock_session = AsyncMock()
        request = MagicMock()
        request.body = AsyncMock(return_value=b"{}")

        resource = await resolve_trigger_run_team_scope(request, mock_session)
        assert resource is None

    @pytest.mark.asyncio
    async def test_resolver_returns_none_for_invalid_uuid(self) -> None:
        """Resolver returns None for a non-UUID pipeline_id."""
        from modulo.api.team_scope import resolve_trigger_run_team_scope

        mock_session = AsyncMock()
        request = MagicMock()
        request.body = AsyncMock(return_value=b'{"pipeline_id": "not-a-uuid"}')

        resource = await resolve_trigger_run_team_scope(request, mock_session)
        assert resource is None
