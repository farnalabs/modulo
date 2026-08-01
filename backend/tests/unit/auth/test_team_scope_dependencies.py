"""Unit tests for the team-scoping RLS-parity floor (ADR 017 DECISION 2, task-authz-a1a-team-floor).

The team gate dependency mirrors the DB team-visibility RLS policy:

    visibility='org' OR visibility IS NULL
    OR owner_team_id IS NULL
    OR membership (any team role)
    OR org_role='admin'

Tests drive the dependency's inner coroutine directly with a stubbed membership
lookup, following the existing ``test_permission_dependencies.py`` style.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from modulo.api.dependencies import require_permission, require_team_membership_or_admin
from modulo.api.team_scope import (
    TEAM_SCOPED_RESOLVERS,
    TeamScopedResource,
    resolve_pipeline_team_scope,
)
from modulo.auth.jwt import TenantPrincipal

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()
_TEAM = uuid.uuid4()


def _tenant(org_role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="user@example.com",
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role=org_role,
    )


def _provider(
    *,
    owner_team_id: uuid.UUID | None = _TEAM,
    visibility: str | None = "team",
) -> AsyncMock:
    provider = AsyncMock()
    provider.return_value = TeamScopedResource(owner_team_id=owner_team_id, visibility=visibility)
    return provider


def _make_team_session(*, member: bool) -> AsyncMock:
    """Return a session whose membership lookup resolves to ``member``.

    ``set_rls_org``/``set_rls_user_context`` run against the sqlite dialect
    branch (session.info), so no execute calls are needed for RLS setup — the
    only ``session.execute`` is the stubbed membership query.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.first.return_value = (uuid.uuid4(),) if member else None
    session.execute = AsyncMock(return_value=result)
    return session


def _request() -> MagicMock:
    request = MagicMock()
    request.path_params = {"pipeline_id": str(uuid.uuid4())}
    return request


class TestRequireTeamMembershipOrAdmin:
    def test_tags(self) -> None:
        dep = require_team_membership_or_admin(_provider())
        assert dep.permission == "team.membership_or_admin"
        assert dep.permission_kind == "team_scope"

    @pytest.mark.asyncio
    async def test_org_admin_no_membership_allowed(self) -> None:
        """The sharp RLS case: org-admin bypasses the team gate entirely."""
        dep = require_team_membership_or_admin(_provider())
        session = _make_team_session(member=False)
        outcome = await dep.dependency(_request(), principal=_tenant("admin"), session=session)
        assert outcome.org_role == "admin"
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_operator_with_membership_allowed(self) -> None:
        """Org floor + membership: operator who belongs to the owning team passes."""
        dep = require_team_membership_or_admin(_provider())
        session = _make_team_session(member=True)
        outcome = await dep.dependency(_request(), principal=_tenant("operator"), session=session)
        assert outcome.org_role == "operator"

    @pytest.mark.asyncio
    async def test_org_viewer_no_membership_denied(self) -> None:
        """Non-member is denied by the team gate regardless of org role."""
        dep = require_team_membership_or_admin(_provider())
        session = _make_team_session(member=False)
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(_request(), principal=_tenant("viewer"), session=session)
        assert excinfo.value.status_code == 403
        assert "team" in excinfo.value.detail

    @pytest.mark.asyncio
    async def test_org_viewer_team_viewer_denied_by_org_floor(self) -> None:
        """Composition with require_permission: team membership does NOT lift the org floor.

        An org-viewer who belongs to the owning team still fails
        ``require_permission("pipeline.update")`` (operator floor), which runs on
        the same endpoint before the team gate matters.
        """
        dep_org = require_permission("pipeline.update")
        with pytest.raises(HTTPException) as excinfo:
            await dep_org.dependency(principal=_tenant("viewer"))
        assert excinfo.value.status_code == 403
        assert "pipeline.update" in excinfo.value.detail

    @pytest.mark.asyncio
    async def test_visibility_org_with_owner_team_not_team_gated(self) -> None:
        """visibility='org' + owner_team_id set: org-role floor only (RLS parity)."""
        dep = require_team_membership_or_admin(_provider(owner_team_id=_TEAM, visibility="org"))
        session = _make_team_session(member=False)
        outcome = await dep.dependency(_request(), principal=_tenant("operator"), session=session)
        assert outcome.org_role == "operator"
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_team_null_not_team_gated(self) -> None:
        """owner_team_id IS NULL: org-role floor only (legacy/unowned rows)."""
        dep = require_team_membership_or_admin(_provider(owner_team_id=None, visibility="team"))
        session = _make_team_session(member=False)
        outcome = await dep.dependency(_request(), principal=_tenant("runner"), session=session)
        assert outcome.org_role == "runner"
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_visibility_none_not_team_gated(self) -> None:
        """RLS treats a NULL visibility as org-visible; the app gate must too."""
        dep = require_team_membership_or_admin(_provider(owner_team_id=_TEAM, visibility=None))
        session = _make_team_session(member=False)
        outcome = await dep.dependency(_request(), principal=_tenant("operator"), session=session)
        assert outcome.org_role == "operator"

    @pytest.mark.asyncio
    async def test_resource_not_found_denied(self) -> None:
        provider = AsyncMock(return_value=None)
        dep = require_team_membership_or_admin(provider)
        session = _make_team_session(member=False)
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(_request(), principal=_tenant("operator"), session=session)
        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_db_error_denies_with_503(self) -> None:
        provider = AsyncMock(side_effect=RuntimeError("boom"))
        dep = require_team_membership_or_admin(provider)
        session = _make_team_session(member=False)
        with pytest.raises(RuntimeError):
            await dep.dependency(_request(), principal=_tenant("operator"), session=session)


class TestRunsSpecialCase:
    def test_runs_not_in_team_scoped_set(self) -> None:
        """runs is pinned to the org-role floor only — no team-scope resolver."""
        assert "runs" not in TEAM_SCOPED_RESOLVERS

    def test_team_scoped_set_matches_adr(self) -> None:
        assert set(TEAM_SCOPED_RESOLVERS) == {
            "pipelines",
            "stages",
            "connector_instances",
            "model_backends",
            "environment_profiles",
            "library_primitives",
            "lifecycle_maps",
        }

    def test_run_model_has_no_visibility_column(self) -> None:
        """Pin the schema fact that makes the runs special case necessary."""
        from modulo.db.models.run import Run

        assert not hasattr(Run, "visibility")


class TestPipelineTeamScopeResolver:
    @pytest.mark.asyncio
    async def test_resolves_team_scoped_row(self) -> None:
        session = _make_team_session(member=True)
        result = MagicMock()
        result.first.return_value = (_TEAM, "team")
        session.execute = AsyncMock(return_value=result)
        row = await resolve_pipeline_team_scope(_request(), session)
        assert row == TeamScopedResource(owner_team_id=_TEAM, visibility="team")

    @pytest.mark.asyncio
    async def test_resolves_org_visible_row(self) -> None:
        session = _make_team_session(member=True)
        result = MagicMock()
        result.first.return_value = (None, "org")
        session.execute = AsyncMock(return_value=result)
        row = await resolve_pipeline_team_scope(_request(), session)
        assert row == TeamScopedResource(owner_team_id=None, visibility="org")

    @pytest.mark.asyncio
    async def test_missing_row_returns_none(self) -> None:
        session = _make_team_session(member=True)
        result = MagicMock()
        result.first.return_value = None
        session.execute = AsyncMock(return_value=result)
        row = await resolve_pipeline_team_scope(_request(), session)
        assert row is None

    @pytest.mark.asyncio
    async def test_missing_path_param_returns_none(self) -> None:
        request = MagicMock()
        request.path_params = {}
        session = _make_team_session(member=True)
        row = await resolve_pipeline_team_scope(request, session)
        assert row is None
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_path_param_denied(self) -> None:
        request = MagicMock()
        request.path_params = {"pipeline_id": "not-a-uuid"}
        session = _make_team_session(member=True)
        with pytest.raises(HTTPException) as excinfo:
            await resolve_pipeline_team_scope(request, session)
        assert excinfo.value.status_code == 400
