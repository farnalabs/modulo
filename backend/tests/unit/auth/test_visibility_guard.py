"""Unit tests for the pipeline visibility/owner_team_id transition guard.

task-authz-b-visibility-guard: ``update_pipeline_endpoint`` (PATCH
/api/v1/pipelines/{id}) re-validates the team gate against the NEW
visibility/owner_team_id values before writing, so a team member cannot
downgrade a team-private pipeline to org-visible or reassign it to a team they
don't belong to.

The helper mirrors the RLS-parity membership-or-admin gate (ADR 017 DECISION 2)
against BOTH the current owner team (if the row is currently team-private) and
any new owner team, with the org-admin bypass applying throughout. Tests drive
the helper directly with a stubbed membership lookup, following the
``test_team_scope_dependencies.py`` style.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from modulo.api.routes.pipelines import _assert_team_transition_allowed
from modulo.auth.jwt import TenantPrincipal

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()
_TEAM_A = uuid.uuid4()
_TEAM_B = uuid.uuid4()


def _tenant(org_role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="user@example.com",
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role=org_role,
    )


def _pipeline(*, owner_team_id: uuid.UUID | None, visibility: str) -> SimpleNamespace:
    return SimpleNamespace(owner_team_id=owner_team_id, visibility=visibility)


def _stub_membership(monkeypatch: pytest.MonkeyPatch, teams: set[uuid.UUID]) -> AsyncMock:
    """Patch the route module's team_membership_exists to answer per-team."""

    async def _exists(session, *, account_id: uuid.UUID, team_id: uuid.UUID) -> bool:
        return team_id in teams

    stub = AsyncMock(side_effect=_exists)
    monkeypatch.setattr("modulo.api.routes.pipelines.team_membership_exists", stub)
    return stub


class TestAssertTeamTransitionAllowed:
    @pytest.mark.asyncio
    async def test_member_downgrades_own_team_private_pipeline_allowed(self, monkeypatch) -> None:
        """visibility 'team'→'org' on a team the caller belongs to is allowed."""
        stub = _stub_membership(monkeypatch, {_TEAM_A})
        current = _pipeline(owner_team_id=_TEAM_A, visibility="team")
        await _assert_team_transition_allowed(AsyncMock(), _tenant("operator"), current, {"visibility": "org"})
        # The current-team gate must have run and approved membership once.
        stub.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_member_downgrades_different_teams_pipeline_denied(self, monkeypatch) -> None:
        """visibility 'team'→'org' on a DIFFERENT team's pipeline is denied (403)."""
        _stub_membership(monkeypatch, {_TEAM_B})
        current = _pipeline(owner_team_id=_TEAM_A, visibility="team")
        with pytest.raises(HTTPException) as excinfo:
            await _assert_team_transition_allowed(AsyncMock(), _tenant("operator"), current, {"visibility": "org"})
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_non_member_reassign_to_foreign_team_denied(self, monkeypatch) -> None:
        """Reassigning owner_team_id to a team the caller is not in is denied (403)."""
        _stub_membership(monkeypatch, {_TEAM_A})
        current = _pipeline(owner_team_id=_TEAM_A, visibility="team")
        with pytest.raises(HTTPException) as excinfo:
            await _assert_team_transition_allowed(AsyncMock(), _tenant("operator"), current, {"owner_team_id": _TEAM_B})
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_member_reassign_to_own_team_allowed(self, monkeypatch) -> None:
        """Reassigning owner_team_id to a team the caller belongs to is allowed."""
        stub = _stub_membership(monkeypatch, {_TEAM_A})
        current = _pipeline(owner_team_id=None, visibility="org")
        await _assert_team_transition_allowed(AsyncMock(), _tenant("operator"), current, {"owner_team_id": _TEAM_A})
        # The new-team gate must have run and approved membership once.
        stub.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_owner_team_with_team_visibility_rejected(self, monkeypatch) -> None:
        """Clearing owner_team_id while visibility stays 'team' is a 422."""
        _stub_membership(monkeypatch, {_TEAM_A})
        current = _pipeline(owner_team_id=_TEAM_A, visibility="team")
        with pytest.raises(HTTPException) as excinfo:
            await _assert_team_transition_allowed(AsyncMock(), _tenant("operator"), current, {"owner_team_id": None})
        assert excinfo.value.status_code == 422

    @pytest.mark.asyncio
    async def test_clear_owner_team_while_switching_to_org_allowed(self, monkeypatch) -> None:
        """Clearing owner_team_id while ALSO switching visibility to 'org' is valid."""
        stub = _stub_membership(monkeypatch, {_TEAM_A})
        current = _pipeline(owner_team_id=_TEAM_A, visibility="team")
        await _assert_team_transition_allowed(
            AsyncMock(), _tenant("operator"), current, {"visibility": "org", "owner_team_id": None}
        )
        # Only the current-team gate runs (owner_team_id is cleared, not reassigned).
        stub.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_org_admin_can_do_all_transitions(self, monkeypatch) -> None:
        """The org-admin bypass applies to every transition (RLS parity)."""
        stub = _stub_membership(monkeypatch, set())
        admin = _tenant("admin")
        await _assert_team_transition_allowed(
            AsyncMock(),
            admin,
            _pipeline(owner_team_id=_TEAM_A, visibility="team"),
            {"visibility": "org"},
        )
        await _assert_team_transition_allowed(
            AsyncMock(),
            admin,
            _pipeline(owner_team_id=_TEAM_A, visibility="team"),
            {"owner_team_id": _TEAM_B},
        )
        await _assert_team_transition_allowed(
            AsyncMock(),
            admin,
            _pipeline(owner_team_id=_TEAM_A, visibility="team"),
            {"owner_team_id": None},
        )
        await _assert_team_transition_allowed(
            AsyncMock(),
            admin,
            _pipeline(owner_team_id=None, visibility="org"),
            {"owner_team_id": _TEAM_B},
        )
        stub.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_visibility_owner_change_is_noop(self, monkeypatch) -> None:
        """Updates that don't touch visibility/owner_team_id pass without checks."""
        stub = _stub_membership(monkeypatch, set())
        current = _pipeline(owner_team_id=_TEAM_A, visibility="team")
        await _assert_team_transition_allowed(
            AsyncMock(), _tenant("operator"), current, {"name": "renamed", "max_concurrent_runs": 3}
        )
        # No boundary change -> the membership check must not run at all.
        stub.assert_not_awaited()
