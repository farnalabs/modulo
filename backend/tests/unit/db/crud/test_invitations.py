"""Unit tests for the Invitation CRUD surface (FAR-461).

The API route tests in ``tests/unit/api/test_admin_invites.py`` and
``tests/unit/auth/test_accept_invite.py`` mock these CRUD functions, so the
real implementations in ``db/crud/invitations.py`` and
``db/crud/org_membership.reactivate_membership`` need their own direct
coverage.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.invitations import (
    consume_invitation,
    create_invitation,
    get_valid_by_token_hash,
    has_live_for_email,
    hash_invitation_token,
    list_pending_for_org,
    revoke_invitation,
)
from modulo.db.crud.org_membership import reactivate_membership
from modulo.db.models.invitation import Invitation


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _invitation() -> Invitation:
    return Invitation(
        organisation_id=uuid.uuid4(),
        email="invited@example.com",
        display_name="Invited User",
        org_role="runner",
        token_hash="x" * 64,
        invited_by=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )


class TestHashInvitationToken:
    def test_delegates_to_hash_token(self) -> None:
        from modulo.util.one_time_token import hash_token

        assert hash_invitation_token("tok") == hash_token("tok")


class TestCreateInvitation:
    async def test_returns_invitation_and_plaintext(self) -> None:
        session = _mock_session()
        org_id = uuid.uuid4()
        invited_by = uuid.uuid4()
        expires_at = datetime.now(UTC) + timedelta(days=3)

        invitation, plaintext = await create_invitation(
            session,
            organisation_id=org_id,
            email="new@example.com",
            display_name="New Person",
            org_role="runner",
            invited_by=invited_by,
            expires_at=expires_at,
        )

        assert isinstance(invitation, Invitation)
        assert isinstance(plaintext, str) and len(plaintext) >= 32
        # The persisted hash is the SHA-256 of the returned plaintext.
        from modulo.util.one_time_token import hash_token

        assert invitation.token_hash == hash_token(plaintext)
        assert invitation.organisation_id == org_id
        assert invitation.invited_by == invited_by
        session.add.assert_called_once_with(invitation)
        session.flush.assert_awaited_once()


class TestHasLiveForEmail:
    async def test_true_when_a_live_invitation_exists(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=uuid.uuid4())))

        assert await has_live_for_email(session, org_id=uuid.uuid4(), email="a@b.com") is True

    async def test_false_when_no_live_invitation(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        assert await has_live_for_email(session, org_id=uuid.uuid4(), email="a@b.com") is False


class TestGetValidByTokenHash:
    async def test_returns_invitation_when_live(self) -> None:
        session = _mock_session()
        invitation = _invitation()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=invitation)))

        assert await get_valid_by_token_hash(session, "hash") is invitation

    async def test_returns_none_when_no_live_match(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        assert await get_valid_by_token_hash(session, "hash") is None


class TestConsumeInvitation:
    async def test_true_when_row_was_consumed(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

        assert await consume_invitation(session, _invitation()) is True

    async def test_false_when_already_spent(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=0))

        assert await consume_invitation(session, _invitation()) is False


class TestRevokeInvitation:
    async def test_true_when_row_was_revoked(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

        assert await revoke_invitation(session, invitation_id=uuid.uuid4(), org_id=uuid.uuid4()) is True

    async def test_false_when_no_matching_row(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=0))

        assert await revoke_invitation(session, invitation_id=uuid.uuid4(), org_id=uuid.uuid4()) is False


class TestListPendingForOrg:
    async def test_returns_items_and_total(self) -> None:
        session = _mock_session()
        items = [_invitation(), _invitation()]
        count_result = MagicMock(scalar=MagicMock(return_value=2))
        scalars = MagicMock(all=MagicMock(return_value=items))
        items_result = MagicMock(scalars=MagicMock(return_value=scalars))
        session.execute = AsyncMock(side_effect=[count_result, items_result])

        result, total = await list_pending_for_org(session, org_id=uuid.uuid4())

        assert result == items
        assert total == 2

    async def test_empty_list(self) -> None:
        session = _mock_session()
        count_result = MagicMock(scalar=MagicMock(return_value=0))
        scalars = MagicMock(all=MagicMock(return_value=[]))
        items_result = MagicMock(scalars=MagicMock(return_value=scalars))
        session.execute = AsyncMock(side_effect=[count_result, items_result])

        result, total = await list_pending_for_org(session, org_id=uuid.uuid4(), page=2, page_size=10)

        assert result == []
        assert total == 0

    async def test_pagination_offsets_by_page(self) -> None:
        session = _mock_session()
        count_result = MagicMock(scalar=MagicMock(return_value=100))
        scalars = MagicMock(all=MagicMock(return_value=[_invitation()]))
        items_result = MagicMock(scalars=MagicMock(return_value=scalars))
        session.execute = AsyncMock(side_effect=[count_result, items_result])

        _, total = await list_pending_for_org(session, org_id=uuid.uuid4(), page=3, page_size=25)

        assert total == 100
        # Second query (the items query) must carry an OFFSET clause whose
        # bound value is (page-1)*page_size = 50.
        items_query = session.execute.call_args_list[1].args[0]
        assert "OFFSET" in str(items_query).upper()
        compiled = items_query.compile()
        assert list(compiled.params.values()).count(50) == 1


class TestReactivateMembership:
    async def test_clears_deactivated_at_and_sets_role(self) -> None:
        session = _mock_session()
        membership = MagicMock()
        membership.deactivated_at = "2026-08-01T00:00:00+00:00"
        membership.role = "viewer"

        result = await reactivate_membership(session, membership, "runner")

        assert result.deactivated_at is None
        assert result.role == "runner"
        session.flush.assert_awaited_once()
