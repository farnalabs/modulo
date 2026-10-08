"""Unit tests for the single-sourced team-gate matrix (FAR-1513).

``modulo.api.team_scope.evaluate_team_gate`` is ONE body shared by:
  - the REST dependency ``require_team_membership_or_admin[_any_credential]``
    (``modulo.api.dependencies``), and
  - the MCP per-row pipeline/trigger guards (``mcp_server._pipeline_team_gate``),
    which evaluate it against team-blind-resolved rows.

This file locks the matrix semantics the REST surface relies on: the
not-found-first denial precedence (fail closed even for admins), the
team-scoped-key boundary (evaluated only on team-private rows, 403), the
admin bypass, the membership-or-admin gate on team-private rows, and the
unset-identity fail-closed row. The MCP envelope mapping (the
``team_boundary_violation`` tool results) is covered in
``tests/unit/mcp/test_team_scope_enforcement.py``; the RLS-parity
end-to-end behaviour is covered by the integration suites (Docker-gated).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status

from modulo.api.team_scope import TeamGateDenial, evaluate_team_gate

_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


async def _deny(
    *,
    row_present: bool = True,
    owner: uuid.UUID | None = None,
    visibility: str | None = None,
    account_id: uuid.UUID | None = _OWNER,
    org_role: str | None = "operator",
    team_key: uuid.UUID | None = None,
    member: bool = False,
) -> TeamGateDenial | None:
    """Evaluate the gate with the membership query patched to ``member``."""
    session = AsyncMock()
    with patch("modulo.api.team_scope.team_membership_exists", AsyncMock(return_value=member)):
        return await evaluate_team_gate(
            session,
            row_present=row_present,
            owner_team_id=owner,
            visibility=visibility,
            account_id=account_id,
            org_role=org_role,
            team_key_id=team_key,
        )


class TestNotFoundFirstPrecedence:
    """An absent row denies BEFORE any principal rule can allow it."""

    async def test_absent_row_denies_even_admin(self) -> None:
        # not_found precedes the admin bypass: a missing row is still a denial.
        denial = await _deny(row_present=False, org_role="admin")
        assert denial is not None
        assert denial.kind == "not_found"
        assert denial.status_code == status.HTTP_404_NOT_FOUND

    async def test_absent_row_denies_team_member(self) -> None:
        denial = await _deny(row_present=False, owner=_TEAM_A, visibility="team", member=True)
        assert denial is not None
        assert denial.kind == "not_found"


class TestAdminAndOrgLevelRows:
    async def test_org_admin_bypasses_team_private_row(self) -> None:
        assert await _deny(owner=_TEAM_A, visibility="team", org_role="admin") is None

    async def test_org_visible_row_passes_for_any_role(self) -> None:
        assert await _deny(owner=_TEAM_A, visibility="org") is None

    async def test_null_visibility_row_passes_for_any_role(self) -> None:
        assert await _deny(owner=_TEAM_A, visibility=None) is None

    async def test_ownerless_row_passes_for_any_role(self) -> None:
        assert await _deny(owner=None, visibility="team") is None


class TestTeamKeyBoundary:
    """REST semantics: the key boundary is evaluated on team-private rows."""

    async def test_key_on_own_team_row_passes(self) -> None:
        result = await _deny(owner=_TEAM_A, visibility="team", team_key=_TEAM_A)
        assert result is None

    async def test_key_on_other_teams_row_is_boundary_denial(self) -> None:
        denial = await _deny(owner=_TEAM_A, visibility="team", team_key=_TEAM_B)
        assert denial is not None
        assert denial.kind == "boundary"
        assert denial.status_code == status.HTTP_403_FORBIDDEN
        assert str(_TEAM_A) in denial.detail
        assert str(_TEAM_B) in denial.detail
        assert denial.owner_team_id == _TEAM_A

    async def test_key_boundary_not_evaluated_on_org_visible_row(self) -> None:
        # A different-team key still opens an org-visible row (the org-role
        # floor is the route's own permission dependency, not this gate).
        result = await _deny(owner=_TEAM_A, visibility="org", team_key=_TEAM_B)
        assert result is None


class TestUserMembershipGate:
    async def test_member_passes(self) -> None:
        result = await _deny(owner=_TEAM_A, visibility="team", member=True)
        assert result is None

    async def test_non_member_denied(self) -> None:
        denial = await _deny(owner=_TEAM_A, visibility="team", member=False)
        assert denial is not None
        assert denial.kind == "membership"
        assert denial.status_code == status.HTTP_403_FORBIDDEN
        assert denial.owner_team_id == _TEAM_A

    async def test_unset_user_identity_denied_fail_closed(self) -> None:
        denial = await _deny(owner=_TEAM_A, visibility="team", account_id=None, member=True)
        assert denial is not None
        assert denial.kind == "membership"

    async def test_unknown_visibility_fails_closed(self) -> None:
        # An unrecognised visibility value must NOT skip the membership gate.
        denial = await _deny(owner=_TEAM_A, visibility="legacy", member=False)
        assert denial is not None
        assert denial.kind == "membership"


@pytest.mark.asyncio
async def test_denial_kinds() -> None:
    """The fail-closed vocabulary is exactly not_found / boundary / membership."""
    absent = await _deny(row_present=False)
    boundary = await _deny(owner=_TEAM_A, visibility="team", team_key=_TEAM_B)
    non_member = await _deny(owner=_TEAM_A, visibility="team")

    assert absent is not None
    assert absent.kind == "not_found"
    assert boundary is not None
    assert boundary.kind == "boundary"
    assert non_member is not None
    assert non_member.kind == "membership"


async def test_membership_read_only_for_gated_rows() -> None:
    """The gate must not hit the membership table for rows it never gates."""
    session = MagicMock()
    with patch("modulo.api.team_scope.team_membership_exists", new_callable=AsyncMock) as mock_member:
        result = await evaluate_team_gate(
            session,
            row_present=True,
            owner_team_id=None,
            visibility="org",
            account_id=_OWNER,
            org_role="operator",
            team_key_id=None,
        )

    assert result is None
    mock_member.assert_not_awaited()
