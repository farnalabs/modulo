"""Unit tests for Team CRUD operations."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.base import PageResult


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
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
    t.description = overrides.get("description")
    t.account_id = overrides.get("account_id", _USER_ID)
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
                account_id=_USER_ID,
            )

            mock_team.assert_called_once_with(
                organisation_id=_ORG_ID,
                name="New Team",
                account_id=_USER_ID,
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
                account_id=_USER_ID,
                description="A description",
            )

            mock_team.assert_called_once_with(
                organisation_id=_ORG_ID,
                name="Team with Desc",
                account_id=_USER_ID,
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
    async def test_soft_deletes_and_returns_true(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=team)))

        from modulo.db.crud.team import delete_team

        result = await delete_team(mock_session, _TEAM_ID)
        assert result is True
        assert team.deleted_at is not None
        mock_session.delete.assert_not_called()
        mock_session.flush.assert_awaited_once()

    async def test_returns_false_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.team import delete_team

        result = await delete_team(mock_session, uuid.uuid4())
        assert result is False


class TestReassignTeamResourcesToOrg:
    """reassign_team_resources_to_org bulk UPDATE contract (PRD §9.3)."""

    async def test_clears_owner_team_and_flips_visibility_to_org(self, mock_session: AsyncMock) -> None:
        """Each UPDATE must clear owner_team_id AND flip visibility to 'org'.

        Clearing ownership without flipping visibility would violate the
        ``ck_*_team_owner`` CHECK constraints (``visibility = 'org' OR
        owner_team_id IS NOT NULL``) on real team-private rows.
        """
        from sqlalchemy.sql import Update

        updates: list[Update] = []
        rowcounts = iter([3, 1, 0, 2])

        async def _execute(stmt, *_args: object, **_kwargs: object):
            if isinstance(stmt, Update):
                updates.append(stmt)
            return MagicMock(rowcount=next(rowcounts, 0))

        mock_session.execute = AsyncMock(side_effect=_execute)

        from modulo.db.crud.team import reassign_team_resources_to_org

        count, touched = await reassign_team_resources_to_org(
            mock_session,
            org_id=_ORG_ID,
            team_id=_TEAM_ID,
        )
        assert count == 6
        assert touched == ["pipeline", "connector", "library primitive"]
        assert len(updates) == 4
        for stmt in updates:
            values = {column.key: bind.value for column, bind in stmt._values.items()}
            assert values["owner_team_id"] is None
            assert values["visibility"] == "org"

    async def test_idempotent_when_team_owns_nothing(self, mock_session: AsyncMock) -> None:
        from sqlalchemy.sql import Update

        async def _execute(stmt, *_args: object, **_kwargs: object):
            if isinstance(stmt, Update):
                return MagicMock(rowcount=0)
            return MagicMock(rowcount=0)

        mock_session.execute = AsyncMock(side_effect=_execute)

        from modulo.db.crud.team import reassign_team_resources_to_org

        count, touched = await reassign_team_resources_to_org(
            mock_session,
            org_id=_ORG_ID,
            team_id=_TEAM_ID,
        )
        assert count == 0
        assert touched == []


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


class TestCountOwnedResources:
    async def test_sums_across_all_four_resource_types(self, mock_session: AsyncMock) -> None:
        """count_owned_resources sums pipelines, connectors, model backends, and library primitives."""
        from modulo.db.crud.team import count_owned_resources

        team_a = uuid.uuid4()
        team_b = uuid.uuid4()

        def _result(pairs: list[tuple[uuid.UUID, int]]):
            r = MagicMock()
            r.all = MagicMock(return_value=pairs)
            return r

        mock_session.execute = AsyncMock(
            side_effect=[
                _result([(team_a, 2), (team_b, 1)]),  # pipelines
                _result([(team_a, 1)]),  # connectors
                _result([(team_a, 3)]),  # model backends
                _result([(team_b, 4)]),  # library primitives
            ]
        )

        counts = await count_owned_resources(mock_session, team_ids=[team_a, team_b])
        assert counts[team_a] == 6
        assert counts[team_b] == 5

    async def test_returns_empty_dict_for_no_teams(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.team import count_owned_resources

        counts = await count_owned_resources(mock_session, team_ids=[])
        assert counts == {}
        mock_session.execute.assert_not_awaited()

    async def test_ignores_null_owner_rows(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.team import count_owned_resources

        def _result(pairs: list[tuple[uuid.UUID | None, int]]):
            r = MagicMock()
            r.all = MagicMock(return_value=pairs)
            return r

        mock_session.execute = AsyncMock(
            side_effect=[
                _result([(None, 99)]),  # pipelines with null owner
                _result([]),
                _result([]),
                _result([]),
            ]
        )

        counts = await count_owned_resources(mock_session, team_ids=[uuid.uuid4()])
        assert counts == {}


class TestScimListGroups:
    async def test_excludes_soft_deleted_teams(self, mock_session: AsyncMock) -> None:
        """scim_list_groups must filter deleted_at IS NULL on both the count and page query."""
        count_result = MagicMock()
        count_result.scalar = MagicMock(return_value=1)
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[_make_team()])
        mock_session.execute = AsyncMock(
            side_effect=[
                count_result,
                MagicMock(scalars=MagicMock(return_value=scalars)),
            ]
        )

        from modulo.db.crud.scim import scim_list_groups

        items, total = await scim_list_groups(mock_session, _ORG_ID)
        assert total == 1
        assert len(items) == 1

        assert len(mock_session.execute.await_args_list) == 2
        for call in mock_session.execute.await_args_list:
            sql = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert "deleted_at IS NULL" in sql
            assert f"organisation_id = '{_ORG_ID.hex}'" in sql

    async def test_excludes_soft_deleted_teams_with_filter(self, mock_session: AsyncMock) -> None:
        """The deleted_at filter composes with an optional name filter."""
        count_result = MagicMock()
        count_result.scalar = MagicMock(return_value=1)
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[_make_team()])
        mock_session.execute = AsyncMock(
            side_effect=[
                count_result,
                MagicMock(scalars=MagicMock(return_value=scalars)),
            ]
        )

        from modulo.db.crud.scim import scim_list_groups

        items, total = await scim_list_groups(mock_session, _ORG_ID, filter_str="foo")
        assert total == 1
        assert len(items) == 1

        for call in mock_session.execute.await_args_list:
            sql = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert "deleted_at IS NULL" in sql
            assert "LIKE" in sql
