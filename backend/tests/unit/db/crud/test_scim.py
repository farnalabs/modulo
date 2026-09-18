"""Unit tests for SCIM provisioning CRUD (mocked session, no DB)."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_account(**overrides: Any) -> MagicMock:
    account = MagicMock()
    account.id = overrides.get("id", _ACCOUNT_ID)
    account.email = overrides.get("email", "scim-user@example.com")
    account.display_name = overrides.get("display_name", "SCIM User")
    account.active = overrides.get("active", True)
    account.password_hash = overrides.get("password_hash", "old-hash")
    return account


def _make_membership(**overrides: Any) -> MagicMock:
    membership = MagicMock()
    membership.id = overrides.get("id", uuid.uuid4())
    membership.account_id = overrides.get("account_id", _ACCOUNT_ID)
    membership.organisation_id = overrides.get("organisation_id", _ORG_ID)
    membership.role = overrides.get("role", "runner")
    membership.deactivated_at = overrides.get("deactivated_at")
    return membership


def _make_team(**overrides: Any) -> MagicMock:
    team = MagicMock()
    team.id = overrides.get("id", _TEAM_ID)
    team.organisation_id = _ORG_ID
    team.name = overrides.get("name", "It Team")
    team.deleted_at = None
    return team


def _count_result(total: int) -> MagicMock:
    result = MagicMock()
    result.scalar = MagicMock(return_value=total)
    return result


def _listing_result(items: list[Any]) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars)
    return result


class TestScimCreateUser:
    async def test_new_account_created_with_membership(self, mock_session: AsyncMock) -> None:
        new_account = _make_account()
        with (
            patch("modulo.db.crud.scim.get_account_by_email", AsyncMock(return_value=None)),
            patch("modulo.db.crud.scim.get_membership_by_account_and_org", AsyncMock()) as get_membership,
            patch("modulo.db.crud.scim.create_membership", AsyncMock()) as create_membership,
            patch("modulo.db.crud.scim.Account", return_value=new_account) as account_cls,
        ):
            from modulo.db.crud.scim import scim_create_user

            result = await scim_create_user(
                mock_session,
                org_id=_ORG_ID,
                email="new@example.com",
                display_name="New User",
            )

        kwargs = account_cls.call_args.kwargs
        assert kwargs["email"] == "new@example.com"
        assert kwargs["auth_provider"] == "scim"
        assert kwargs["password_hash"] is None
        create_membership.assert_awaited_once_with(
            mock_session,
            account_id=new_account.id,
            org_id=_ORG_ID,
            role="runner",
        )
        get_membership.assert_not_awaited()
        assert result is new_account

    async def test_existing_user_without_membership_gets_one(self, mock_session: AsyncMock) -> None:
        existing = _make_account()
        with (
            patch("modulo.db.crud.scim.get_account_by_email", AsyncMock(return_value=existing)),
            patch("modulo.db.crud.scim.get_membership_by_account_and_org", AsyncMock(return_value=None)),
            patch("modulo.db.crud.scim.create_membership", AsyncMock()) as create_membership,
        ):
            from modulo.db.crud.scim import scim_create_user

            result = await scim_create_user(
                mock_session,
                org_id=_ORG_ID,
                email="scim-user@example.com",
                display_name="SCIM User",
                org_role="admin",
            )
        create_membership.assert_awaited_once()
        call_kwargs = create_membership.call_args.kwargs
        assert call_kwargs["org_id"] == _ORG_ID
        assert call_kwargs["role"] == "admin"
        assert result is existing

    async def test_tombstoned_membership_revived(self, mock_session: AsyncMock) -> None:
        deactivated_at = object()
        membership = _make_membership(deactivated_at=deactivated_at)
        existing = _make_account(password_hash="hash-should-clear")
        existing.active = True
        with (
            patch("modulo.db.crud.scim.get_account_by_email", AsyncMock(return_value=existing)),
            patch(
                "modulo.db.crud.scim.get_membership_by_account_and_org",
                AsyncMock(return_value=membership),
            ),
        ):
            from modulo.db.crud.scim import scim_create_user

            result = await scim_create_user(
                mock_session,
                org_id=_ORG_ID,
                email="scim-user@example.com",
                display_name="SCIM User",
            )
        assert membership.deactivated_at is None
        assert existing.password_hash is None
        assert result is existing

    async def test_existing_active_membership_idempotent(self, mock_session: AsyncMock) -> None:
        membership = _make_membership()
        existing = _make_account()
        with (
            patch("modulo.db.crud.scim.get_account_by_email", AsyncMock(return_value=existing)),
            patch(
                "modulo.db.crud.scim.get_membership_by_account_and_org",
                AsyncMock(return_value=membership),
            ),
        ):
            from modulo.db.crud.scim import scim_create_user

            result = await scim_create_user(
                mock_session,
                org_id=_ORG_ID,
                email="scim-user@example.com",
                display_name="SCIM User",
                org_role="runner",
                active=False,
            )
        assert existing.active is False
        assert result is existing


class TestScimGetUser:
    async def test_returns_account_when_membership_exists(self, mock_session: AsyncMock) -> None:
        account = _make_account()
        with (
            patch("modulo.db.crud.scim.get_account_by_id", AsyncMock(return_value=account)),
            patch(
                "modulo.db.crud.scim.get_membership_by_account_and_org",
                AsyncMock(return_value=MagicMock()),
            ),
        ):
            from modulo.db.crud.scim import scim_get_user

            result = await scim_get_user(mock_session, _ORG_ID, _ACCOUNT_ID)
        assert result is account

    async def test_returns_none_when_account_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.scim.get_account_by_id", AsyncMock(return_value=None)):
            from modulo.db.crud.scim import scim_get_user

            result = await scim_get_user(mock_session, _ORG_ID, _ACCOUNT_ID)
        assert result is None

    async def test_returns_none_when_no_membership_in_org(self, mock_session: AsyncMock) -> None:
        with (
            patch("modulo.db.crud.scim.get_account_by_id", AsyncMock(return_value=_make_account())),
            patch("modulo.db.crud.scim.get_membership_by_account_and_org", AsyncMock(return_value=None)),
        ):
            from modulo.db.crud.scim import scim_get_user

            result = await scim_get_user(mock_session, _ORG_ID, _ACCOUNT_ID)
        assert result is None


class TestScimUpdateUser:
    async def test_applies_updates_and_flushes(self, mock_session: AsyncMock) -> None:
        account = _make_account()
        with patch("modulo.db.crud.scim.apply_updates") as apply_updates:
            from modulo.db.crud.scim import scim_update_user

            result = await scim_update_user(
                mock_session,
                account,
                org_id=_ORG_ID,
                email="changed@example.com",
                display_name="Renamed",
                active=False,
            )
        apply_updates.assert_called_once_with(
            account,
            {"email": "changed@example.com", "display_name": "Renamed", "active": False},
        )
        assert result is account

    async def test_org_role_update_executes_membership_statement(self, mock_session: AsyncMock) -> None:
        account = _make_account()
        with patch("modulo.db.crud.scim.apply_updates"):
            from modulo.db.crud.scim import scim_update_user

            result = await scim_update_user(mock_session, account, org_id=_ORG_ID, org_role="admin")
        assert result is account
        mock_session.execute.assert_awaited_once()

    async def test_no_updates_is_noop(self, mock_session: AsyncMock) -> None:
        account = _make_account()
        from modulo.db.crud.scim import scim_update_user

        result = await scim_update_user(mock_session, account, org_id=_ORG_ID)
        assert result is account


class TestScimDeactivateDelete:
    async def test_deactivate_calls_security_definer_and_refreshes(self, mock_session: AsyncMock) -> None:
        account = _make_account()
        with (
            patch("modulo.db.crud.scim.scim_get_user", AsyncMock(return_value=account)) as get_user,
        ):
            from modulo.db.crud.scim import scim_deactivate_user

            result = await scim_deactivate_user(
                mock_session,
                _ORG_ID,
                _ACCOUNT_ID,
                caller_account_id=_ACCOUNT_ID,
            )
        get_user.assert_awaited_once_with(mock_session, _ORG_ID, _ACCOUNT_ID)
        executed_stmt, params = mock_session.execute.call_args.args
        assert params == {"caller": _ACCOUNT_ID, "target": _ACCOUNT_ID}
        assert str(executed_stmt) == "SELECT public.deactivate_break_glass(:caller, :target, false)"
        mock_session.refresh.assert_awaited_once_with(account)
        assert result is account

    async def test_delete_user_returns_false_when_unknown(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.scim.scim_get_user", AsyncMock(return_value=None)):
            from modulo.db.crud.scim import scim_delete_user_by_id

            result = await scim_delete_user_by_id(mock_session, _ORG_ID, _ACCOUNT_ID, caller_account_id=_ACCOUNT_ID)
        assert result is False
        mock_session.execute.assert_not_awaited()

    async def test_delete_user_tombstones_and_returns_true(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.scim.scim_get_user", AsyncMock(return_value=_make_account())):
            from modulo.db.crud.scim import scim_delete_user_by_id

            result = await scim_delete_user_by_id(mock_session, _ORG_ID, _ACCOUNT_ID, caller_account_id=_ACCOUNT_ID)
        assert result is True
        mock_session.execute.assert_awaited_once()


class TestScimListUsers:
    async def test_lists_users_with_org_scope_and_pagination(self, mock_session: AsyncMock) -> None:
        accounts = [_make_account(), _make_account(email="b@example.com")]
        mock_session.execute = AsyncMock(side_effect=[_count_result(2), _listing_result(accounts)])
        from modulo.db.crud.scim import scim_list_users

        items, total = await scim_list_users(mock_session, _ORG_ID, start_index=11, count=10)
        assert items == accounts
        assert total == 2

    async def test_filter_applies_search(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_count_result(0), _listing_result([])])
        from modulo.db.crud.scim import scim_list_users

        items, total = await scim_list_users(mock_session, _ORG_ID, filter_str="ali")
        assert items == []
        assert total == 0

    async def test_programming_error_returns_empty(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.scim import scim_list_users

        items, total = await scim_list_users(mock_session, _ORG_ID)
        assert items == []
        assert total == 0


class TestScimGroupOperations:
    async def test_create_group_without_account_uses_nil_uuid(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        with patch("modulo.db.crud.scim.create_team", AsyncMock(return_value=team)) as create_team:
            from modulo.db.crud.scim import scim_create_group

            result = await scim_create_group(
                mock_session,
                org_id=_ORG_ID,
                display_name="Idps",
                description="desc",
            )
        create_team.assert_awaited_once_with(
            mock_session,
            org_id=_ORG_ID,
            name="Idps",
            account_id=uuid.UUID(int=0),
            description="desc",
        )
        assert result is team

    async def test_create_group_with_account(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        with (
            patch("modulo.db.crud.scim.create_team", AsyncMock(return_value=team)) as create_team,
        ):
            from modulo.db.crud.scim import scim_create_group

            result = await scim_create_group(
                mock_session,
                org_id=_ORG_ID,
                display_name="Idps",
                account_id=_ACCOUNT_ID,
            )
        assert create_team.call_args.kwargs["account_id"] == _ACCOUNT_ID
        assert result is team

    async def test_get_and_delete_group_delegate(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        with (
            patch("modulo.db.crud.scim.get_team", AsyncMock(return_value=team)) as get_team,
            patch("modulo.db.crud.scim.delete_team", AsyncMock(return_value=True)) as delete_team,
        ):
            from modulo.db.crud.scim import scim_delete_group_by_id, scim_get_group

            assert await scim_get_group(mock_session, _TEAM_ID) is team
            assert await scim_delete_group_by_id(mock_session, _TEAM_ID) is True
        get_team.assert_awaited_once_with(mock_session, _TEAM_ID)
        delete_team.assert_awaited_once_with(mock_session, _TEAM_ID)

    async def test_update_group_no_updates_returns_team(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        from modulo.db.crud.scim import scim_update_group

        assert await scim_update_group(mock_session, team) is team

    async def test_update_group_renames(self, mock_session: AsyncMock) -> None:
        team = _make_team()
        renamed = _make_team(name="Renamed")
        with (
            patch("modulo.db.crud.scim.update_team", AsyncMock(return_value=renamed)) as update_team,
        ):
            from modulo.db.crud.scim import scim_update_group

            result = await scim_update_group(mock_session, team, name="Renamed")
        update_team.assert_awaited_once_with(mock_session, team.id, {"name": "Renamed"})
        assert result is renamed

    async def test_list_groups(self, mock_session: AsyncMock) -> None:
        teams = [_make_team(), _make_team(name="B Team")]
        mock_session.execute = AsyncMock(side_effect=[_count_result(2), _listing_result(teams)])
        from modulo.db.crud.scim import scim_list_groups

        items, total = await scim_list_groups(mock_session, _ORG_ID, filter_str="team", count=5)
        assert items == teams
        assert total == 2

    async def test_list_groups_programming_error(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.scim import scim_list_groups

        items, total = await scim_list_groups(mock_session, _ORG_ID)
        assert not items
        assert total == 0


class TestScimGroupMembership:
    async def test_add_member_reuses_existing_membership(self, mock_session: AsyncMock) -> None:
        existing = _make_membership()
        with (
            patch(
                "modulo.db.crud.scim.get_membership_by_team_and_account",
                AsyncMock(return_value=existing),
            ),
            patch("modulo.db.crud.scim.add_team_member") as add_team_member,
        ):
            from modulo.db.crud.scim import scim_add_group_member

            result = await scim_add_group_member(mock_session, org_id=_ORG_ID, team_id=_TEAM_ID, user_id=_ACCOUNT_ID)
        add_team_member.assert_not_awaited()
        assert result is existing

    async def test_add_member_creates_membership(self, mock_session: AsyncMock) -> None:
        created = _make_membership(role="member")
        with (
            patch(
                "modulo.db.crud.scim.get_membership_by_team_and_account",
                AsyncMock(return_value=None),
            ),
            patch("modulo.db.crud.scim.add_team_member", AsyncMock(return_value=created)) as add_team_member,
        ):
            from modulo.db.crud.scim import scim_add_group_member

            result = await scim_add_group_member(mock_session, org_id=_ORG_ID, team_id=_TEAM_ID, user_id=_ACCOUNT_ID)
        add_team_member.assert_awaited_once_with(
            mock_session,
            org_id=_ORG_ID,
            team_id=_TEAM_ID,
            account_id=_ACCOUNT_ID,
            role="member",
        )
        assert result is created

    async def test_remove_member_missing_returns_false(self, mock_session: AsyncMock) -> None:
        with (
            patch(
                "modulo.db.crud.scim.get_membership_by_team_and_account",
                AsyncMock(return_value=None),
            ),
        ):
            from modulo.db.crud.scim import scim_remove_group_member

            result = await scim_remove_group_member(mock_session, _TEAM_ID, _ACCOUNT_ID)
        assert result is False

    async def test_remove_member_removes_by_membership_id(self, mock_session: AsyncMock) -> None:
        membership = _make_membership()
        with (
            patch(
                "modulo.db.crud.scim.get_membership_by_team_and_account",
                AsyncMock(return_value=membership),
            ),
            patch("modulo.db.crud.scim.remove_team_member", AsyncMock()) as remove_team_member,
        ):
            from modulo.db.crud.scim import scim_remove_group_member

            result = await scim_remove_group_member(mock_session, _TEAM_ID, _ACCOUNT_ID)
        remove_team_member.assert_awaited_once_with(mock_session, membership.id)
        assert result is True

    async def test_list_group_members(self, mock_session: AsyncMock) -> None:
        members = [_make_membership(), _make_membership(account_id=uuid.uuid4())]
        mock_session.execute = AsyncMock(return_value=_listing_result(members))
        from modulo.db.crud.scim import scim_list_group_members

        result = await scim_list_group_members(mock_session, _TEAM_ID)
        assert result == members
