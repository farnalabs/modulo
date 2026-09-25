"""Unit tests for the accountability-owner eligibility invariant (FAR-1161).

Exercises every fail-closed branch of ``validate_accountability_owner`` with a
scripted session (no DB): account missing/inactive, org membership
missing/deactivated, and the ``visibility='team'`` owner-team gate. The
integration suite covers the same invariant against a real RLS session; these
tests pin the branch-level behaviour without a database.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from modulo.db.crud.pipeline_owner import (
    ACCOUNTABILITY_OWNER_FIELDS,
    validate_accountability_owner,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ab")


class _Result:
    """A result whose ``.first()`` returns the scripted row."""

    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        return self._row


def _session(*rows: object | None) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_Result(r) for r in rows])
    return session


async def _expect_422(session: MagicMock, **kwargs: object) -> str:
    with pytest.raises(HTTPException) as exc_info:
        await validate_accountability_owner(session, **kwargs)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 422
    return str(exc_info.value.detail)


class TestFieldConstants:
    def test_accountability_owner_fields(self) -> None:
        assert ACCOUNTABILITY_OWNER_FIELDS == ("business_owner_id", "reliability_owner_id")


class TestNoneOwner:
    async def test_none_short_circuits_without_query(self) -> None:
        session = _session()
        result = await validate_accountability_owner(
            session,
            owner_account_id=None,
            field="business_owner_id",
            org_id=_ORG_ID,
            visibility="team",
            owner_team_id=None,
        )
        assert result is None
        session.execute.assert_not_awaited()


class TestAccountChecks:
    async def test_missing_account_rejected(self) -> None:
        session = _session(None)
        detail = await _expect_422(
            session,
            owner_account_id=_OWNER_ID,
            field="business_owner_id",
            org_id=_ORG_ID,
            visibility="org",
            owner_team_id=None,
        )
        assert "does not exist" in detail
        assert "business_owner_id" in detail

    async def test_inactive_account_rejected(self) -> None:
        session = _session((_OWNER_ID, False))
        detail = await _expect_422(
            session,
            owner_account_id=_OWNER_ID,
            field="reliability_owner_id",
            org_id=_ORG_ID,
            visibility="org",
            owner_team_id=None,
        )
        assert "is deactivated" in detail


class TestOrgMembershipChecks:
    async def test_missing_org_membership_rejected(self) -> None:
        session = _session((_OWNER_ID, True), None)
        detail = await _expect_422(
            session,
            owner_account_id=_OWNER_ID,
            field="business_owner_id",
            org_id=_ORG_ID,
            visibility="org",
            owner_team_id=None,
        )
        assert "is not a member of this organisation" in detail

    async def test_deactivated_org_membership_rejected(self) -> None:
        session = _session((_OWNER_ID, True), (object(),))
        detail = await _expect_422(
            session,
            owner_account_id=_OWNER_ID,
            field="business_owner_id",
            org_id=_ORG_ID,
            visibility="org",
            owner_team_id=None,
        )
        assert "has a deactivated membership in this organisation" in detail

    async def test_active_org_member_accepted(self) -> None:
        session = _session((_OWNER_ID, True), (None,))
        result = await validate_accountability_owner(
            session,
            owner_account_id=_OWNER_ID,
            field="business_owner_id",
            org_id=_ORG_ID,
            visibility="org",
            owner_team_id=None,
        )
        assert result is None


class TestTeamGate:
    async def test_team_visibility_without_owner_team_rejected(self) -> None:
        session = _session((_OWNER_ID, True), (None,))
        detail = await _expect_422(
            session,
            owner_account_id=_OWNER_ID,
            field="business_owner_id",
            org_id=_ORG_ID,
            visibility="team",
            owner_team_id=None,
        )
        assert "requires an owner team" in detail

    async def test_team_visibility_non_member_rejected(self) -> None:
        session = _session((_OWNER_ID, True), (None,), None)
        detail = await _expect_422(
            session,
            owner_account_id=_OWNER_ID,
            field="reliability_owner_id",
            org_id=_ORG_ID,
            visibility="team",
            owner_team_id=_TEAM_ID,
        )
        assert "is not a member of the pipeline's owner team" in detail

    async def test_team_visibility_member_accepted(self) -> None:
        session = _session((_OWNER_ID, True), (None,), (uuid.uuid4(),))
        result = await validate_accountability_owner(
            session,
            owner_account_id=_OWNER_ID,
            field="reliability_owner_id",
            org_id=_ORG_ID,
            visibility="team",
            owner_team_id=_TEAM_ID,
        )
        assert result is None
