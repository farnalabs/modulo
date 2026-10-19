"""ADR 063 step 1 (FAR-1957): the shared human-linkage authority resolver.

Exercises the REAL ``resolve_human_linkage`` against the account/membership DB
seam (``resolve_live_role_and_kind``) patched - the resolver's own decision
logic (conditions a-d + fail-closed behaviour) is what is under test here; the
MCP gate that consumes it is tested in
``tests/unit/mcp/test_hitl_human_linked_authority.py``.
"""

import uuid
from unittest.mock import AsyncMock, patch

from modulo.auth.human_linkage import (
    HITL_ACTION_DEFAULT_PERMISSION,
    LINKAGE_NONE,
    LINKAGE_OAUTH_CONSENT,
    required_permission_for_action,
    resolve_human_linkage,
)
from modulo.db.models.account import KIND_HUMAN, KIND_SERVICE

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000013")
_FAMILY = "oauth-family-1"
_CLAIM_GRANT = frozenset({"hitl.claim"})


def _session() -> AsyncMock:
    return AsyncMock()


async def _resolve(
    *,
    auth_type: str = "oauth",
    account_id: uuid.UUID | None = _ACCOUNT_ID,
    token_family: str | None = _FAMILY,
    grants: frozenset[str] | None = _CLAIM_GRANT,
    permission: str = "hitl.claim",
):
    return await resolve_human_linkage(
        _session(),
        org_id=_ORG_ID,
        auth_type=auth_type,
        account_id=account_id,
        token_family=token_family,
        grants=grants,
        required_permission=permission,
    )


class TestHumanLinkagePositive:
    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_consent_derived_human_oauth_token_is_human_linked(self, mock_membership: AsyncMock) -> None:
        """All four conditions hold ⇒ linked, linkage=oauth_consent, kind=human."""
        mock_membership.return_value = ("operator", KIND_HUMAN)

        linkage = await _resolve()

        assert linkage.linked is True
        assert linkage.linkage == LINKAGE_OAUTH_CONSENT
        assert linkage.principal_kind == KIND_HUMAN
        assert linkage.auth_type == "oauth"
        assert linkage.account_id == _ACCOUNT_ID
        assert linkage.token_family == _FAMILY
        # The account + org are read server-side (never from a client field).
        mock_membership.assert_awaited_once()
        assert mock_membership.await_args.args[1] == str(_ACCOUNT_ID)
        assert mock_membership.await_args.args[2] == str(_ORG_ID)


class TestHumanLinkageNegative:
    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_service_kind_account_is_not_human_linked(self, mock_membership: AsyncMock) -> None:
        """A service principal is structurally incapable of being human-linked."""
        mock_membership.return_value = ("operator", KIND_SERVICE)

        linkage = await _resolve()

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        assert linkage.principal_kind == KIND_SERVICE

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_inactive_or_absent_membership_is_not_human_linked(self, mock_membership: AsyncMock) -> None:
        """No live membership (inactive account / deactivated membership) ⇒ deny."""
        mock_membership.return_value = None

        linkage = await _resolve()

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        assert linkage.principal_kind is None

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_missing_consent_scope_is_not_human_linked(self, mock_membership: AsyncMock) -> None:
        """A live human without the action's consented scope is not linked."""
        mock_membership.return_value = ("operator", KIND_HUMAN)

        linkage = await _resolve(grants=frozenset({"hitl.review"}))

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        assert linkage.principal_kind == KIND_HUMAN

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_no_grant_set_is_not_human_linked(self, mock_membership: AsyncMock) -> None:
        """A credential with no grant-set (legacy/JWT) fails the scope condition."""
        mock_membership.return_value = ("operator", KIND_HUMAN)

        linkage = await _resolve(grants=None)

        assert linkage.linked is False
        assert linkage.principal_kind == KIND_HUMAN

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_api_key_is_excluded_without_a_db_read(self, mock_membership: AsyncMock) -> None:
        """``mk_`` API keys are excluded by construction — no account read."""
        linkage = await _resolve(auth_type="api_key")

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        assert linkage.principal_kind is None
        mock_membership.assert_not_awaited()

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_non_oauth_jwt_is_excluded_without_a_db_read(self, mock_membership: AsyncMock) -> None:
        """A browser-class password JWT (FAR-634) is excluded — no account read."""
        linkage = await _resolve(auth_type="jwt")

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        mock_membership.assert_not_awaited()

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_missing_token_family_is_not_human_linked(self, mock_membership: AsyncMock) -> None:
        """No signed token family ⇒ condition (a) fails, no account read."""
        linkage = await _resolve(token_family=None)

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        mock_membership.assert_not_awaited()

    @patch("modulo.auth.human_linkage.resolve_live_role_and_kind", new_callable=AsyncMock)
    async def test_missing_account_id_is_not_human_linked(self, mock_membership: AsyncMock) -> None:
        linkage = await _resolve(account_id=None)

        assert linkage.linked is False
        assert linkage.linkage == LINKAGE_NONE
        mock_membership.assert_not_awaited()


class TestActionPermissionMapping:
    def test_maps_each_intercepted_action_to_its_registry_key(self) -> None:
        assert required_permission_for_action("claim") == "hitl.claim"
        assert required_permission_for_action("approve") == "hitl.approve"
        assert required_permission_for_action("approve_with_modification") == "hitl.approve"
        assert required_permission_for_action("deliver_manual") == "hitl.deliver_manual"
        assert required_permission_for_action("submit_manual") == "hitl.deliver_manual"
        assert required_permission_for_action("reject") == "hitl.reject"

    def test_unknown_action_falls_to_the_approve_bar(self) -> None:
        assert required_permission_for_action("something-else") == HITL_ACTION_DEFAULT_PERMISSION
