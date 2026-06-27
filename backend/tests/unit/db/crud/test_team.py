"""Unit tests for Team CRUD operations."""

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


def _make_team(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEAM_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Test Team")
    t.description = overrides.get("description", None)
    t.created_by = overrides.get("created_by", _USER_ID)
    t.notification_endpoints = overrides.get("notification_endpoints", [])
    return t


class TestCreateTeam:
    async def test_creates_and_returns_team(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.team.Team", return_value=_make_team()) as mock_team:
            from modulo.db.crud.team import create_team

            result = await create_team(
                mock_session,
                org_id=_ORG_ID,
                name="New Team",
                created_by=_USER_ID,
            )

            mock_team.assert_called_once_with(
                organisation_id=_ORG_ID,
                name="New Team",
                created_by=_USER_ID,
                description=None,
            )
            mock_session.add.assert_called_once()
            mock_session.flush.assert_awaited_once()
            assert result is not None

    async def test_creates_with_optional_description(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.team.Team", return_value=_make_team()) as mock_team:
            from modulo.db.crud.team import create_team

            result = await create_team(
                mock_session,
                org_id=_ORG_ID,
                name="Team with Desc",
                created_by=_USER_ID,
                description="A description",
            )

            mock_team.assert_called_once_with(
                organisation_id=_ORG_ID,
                name="Team with Desc",
                created_by=_USER_ID,
                description="A description",
            )
            assert result is not None


class TestGetTeam:
    async def test_returns_team_when_found(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        scalar = MagicMock(return_value=team)
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=scalar))

        from modulo.db.crud.team import get_team

        result = await get_team(mock_session, _TEAM_ID)
        assert result is not None
        assert result.id == _TEAM_ID

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.team import get_team

        result = await get_team(mock_session, uuid.uuid4())
        assert result is None


class TestListTeams:
    async def test_returns_paginated_teams(self, mock_session: AsyncMock) -> None:
        teams = [_make_team(name="Team A"), _make_team(name="Team B")]

        count_result = MagicMock()
        count_result.scalar = MagicMock(return_value=10)

        scalars = MagicMock()
        scalars.all = MagicMock(return_value=teams)

        mock_session.execute = AsyncMock(
            side_effect=[
                count_result,
                MagicMock(scalars=MagicMock(return_value=scalars)),
            ]
        )

        from modulo.db.crud.team import list_teams

        result = await list_teams(mock_session, org_id=_ORG_ID, page=1, page_size=20)
        assert isinstance(result, PageResult)
        assert len(result.items) == 2
        assert result.total == 10
        assert result.page == 1
        assert result.page_size == 20


class TestUpdateTeam:
    async def test_updates_and_returns_team(self, mock_session: AsyncMock) -> None:
        team = _make_team(name="Updated")
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=team)))

        from modulo.db.crud.team import update_team

        result = await update_team(mock_session, _TEAM_ID, {"name": "Updated"})
        assert result is not None
        assert result.name == "Updated"
        mock_session.flush.assert_awaited_once()

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.team import update_team

        result = await update_team(mock_session, uuid.uuid4(), {"name": "x"})
        assert result is None


class TestDeleteTeam:
    async def test_deletes_and_returns_true(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=team)))

        from modulo.db.crud.team import delete_team

        result = await delete_team(mock_session, _TEAM_ID)
        assert result is True
        mock_session.delete.assert_awaited_once_with(team)
        mock_session.flush.assert_awaited_once()

    async def test_returns_false_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.team import delete_team

        result = await delete_team(mock_session, uuid.uuid4())
        assert result is False


class TestGetTeamByName:
    async def test_returns_team_when_found(self, mock_session: AsyncMock) -> None:
        team = _make_team(name="Unique Team")
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=team)))

        from modulo.db.crud.team import get_team_by_name

        result = await get_team_by_name(mock_session, _ORG_ID, "Unique Team")
        assert result is not None
        assert result.name == "Unique Team"

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.team import get_team_by_name

        result = await get_team_by_name(mock_session, _ORG_ID, "Non Existent")
        assert result is None
