"""Unit tests for TeamMembership CRUD operations."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.base import PageResult


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_MEMBERSHIP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


def _make_membership(**overrides: object) -> MagicMock:
    m = MagicMock()
    m.id = overrides.get("id", _MEMBERSHIP_ID)
    m.organisation_id = overrides.get("organisation_id", _ORG_ID)
    m.team_id = overrides.get("team_id", _TEAM_ID)
    m.user_id = overrides.get("user_id", _USER_ID)
    m.role = overrides.get("role", "member")
    return m


class TestAddMember:
    async def test_adds_and_returns_membership(self, mock_session: AsyncMock) -> None:
        with patch(
            "modulo.db.crud.team_membership.TeamMembership",
            return_value=_make_membership(),
        ) as mock_membership:
            from modulo.db.crud.team_membership import add_team_member

            result = await add_team_member(
                mock_session,
                org_id=_ORG_ID,
                team_id=_TEAM_ID,
                user_id=_USER_ID,
                role="member",
            )

            mock_membership.assert_called_once_with(
                organisation_id=_ORG_ID,
                team_id=_TEAM_ID,
                user_id=_USER_ID,
                role="member",
            )
            mock_session.add.assert_called_once()
            mock_session.flush.assert_awaited_once()
            assert result is not None

    async def test_adds_with_custom_role(self, mock_session: AsyncMock) -> None:
        with patch(
            "modulo.db.crud.team_membership.TeamMembership",
            return_value=_make_membership(role="admin"),
        ) as mock_membership:
            from modulo.db.crud.team_membership import add_team_member

            result = await add_team_member(
                mock_session,
                org_id=_ORG_ID,
                team_id=_TEAM_ID,
                user_id=_USER_ID,
                role="admin",
            )

            mock_membership.assert_called_once_with(
                organisation_id=_ORG_ID,
                team_id=_TEAM_ID,
                user_id=_USER_ID,
                role="admin",
            )
            assert result is not None


class TestGetMembership:
    async def test_returns_membership_when_found(
        self, mock_session: AsyncMock
    ) -> None:
        membership = _make_membership()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=membership)
            )
        )

        from modulo.db.crud.team_membership import get_membership

        result = await get_membership(mock_session, _MEMBERSHIP_ID)
        assert result is not None
        assert result.id == _MEMBERSHIP_ID

    async def test_returns_none_when_not_found(
        self, mock_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=None)
            )
        )

        from modulo.db.crud.team_membership import get_membership

        result = await get_membership(mock_session, uuid.uuid4())
        assert result is None


class TestListTeamMembers:
    async def test_returns_paginated_members(self, mock_session: AsyncMock) -> None:
        members = [
            _make_membership(user_id=uuid.uuid4(), role="member"),
            _make_membership(user_id=uuid.uuid4(), role="admin"),
        ]

        count_result = MagicMock()
        count_result.scalar = MagicMock(return_value=5)

        scalars = MagicMock()
        scalars.all = MagicMock(return_value=members)

        mock_session.execute = AsyncMock(side_effect=[
            count_result,
            MagicMock(scalars=MagicMock(return_value=scalars)),
        ])

        from modulo.db.crud.team_membership import list_team_members

        result = await list_team_members(
            mock_session, team_id=_TEAM_ID, page=1, page_size=20
        )
        assert isinstance(result, PageResult)
        assert len(result.items) == 2
        assert result.total == 5
        assert result.page == 1
        assert result.page_size == 20


class TestUpdateMemberRole:
    async def test_updates_and_returns_membership(
        self, mock_session: AsyncMock
    ) -> None:
        membership = _make_membership(role="admin")
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=membership)
            )
        )

        from modulo.db.crud.team_membership import update_member_role

        result = await update_member_role(
            mock_session, _MEMBERSHIP_ID, "admin"
        )
        assert result is not None
        assert result.role == "admin"

    async def test_returns_none_when_not_found(
        self, mock_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=None)
            )
        )

        from modulo.db.crud.team_membership import update_member_role

        result = await update_member_role(
            mock_session, uuid.uuid4(), "admin"
        )
        assert result is None


class TestRemoveMember:
    async def test_removes_and_returns_true(self, mock_session: AsyncMock) -> None:
        membership = _make_membership()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=membership)
            )
        )

        from modulo.db.crud.team_membership import remove_team_member

        result = await remove_team_member(mock_session, _MEMBERSHIP_ID)
        assert result is True
        mock_session.delete.assert_awaited_once_with(membership)

    async def test_returns_false_when_not_found(
        self, mock_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=None)
            )
        )

        from modulo.db.crud.team_membership import remove_team_member

        result = await remove_team_member(mock_session, uuid.uuid4())
        assert result is False


class TestGetTeamMembershipByTeamAndUser:
    async def test_returns_membership_when_found(
        self, mock_session: AsyncMock
    ) -> None:
        membership = _make_membership()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=membership)
            )
        )

        from modulo.db.crud.team_membership import get_membership_by_team_and_user

        result = await get_membership_by_team_and_user(
            mock_session, _TEAM_ID, _USER_ID
        )
        assert result is not None
        assert result.team_id == _TEAM_ID
        assert result.user_id == _USER_ID

    async def test_returns_none_when_not_found(
        self, mock_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=None)
            )
        )

        from modulo.db.crud.team_membership import get_membership_by_team_and_user

        result = await get_membership_by_team_and_user(
            mock_session, _TEAM_ID, uuid.uuid4()
        )
        assert result is None
