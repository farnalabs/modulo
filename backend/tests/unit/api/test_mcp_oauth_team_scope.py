"""Team-scoped OAuth client enforcement (FAR-1476 slice 1).

The whole point of this slice is that a team-bound OAuth client's tokens feed
the EXISTING team-scoped-credential machinery (``_ctx_team_id`` ->
``_team_scoped_key_mismatch``) with ZERO new per-resource code. These tests
prove that the two OAuth principal-resolution legs (``_finalize_oauth_principal``
on the per-request auth path, ``_validate_oauth_live`` on the SSE per-event
revalidation path) resolve the client's ``team_id`` into ``_ctx_team_id`` so
the existing guards apply.

Before/after evidence (enforcement change):
  BEFORE, both legs did ``_ctx_team_id.set(None)`` ("user tokens carry no team
  boundary"), so ``_team_scoped_key_mismatch`` returned False for every OAuth
  token and a team-bound client could reach another team's resources. AFTER,
  both legs set the client's team, so the same guard rejects a cross-team
  resource. The ``test_..._feeds_existing_team_boundary_guard`` tests assert the
  violation that only exists because of the change — they fail on the old
  ``set(None)`` code.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.api.mcp_server as ms
from modulo.api.mcp_server import (
    _finalize_oauth_principal,
    _validate_oauth_live,
)
from modulo.auth.oauth import OAuthAccessTokenClaims
from modulo.auth.permissions import reset_authz_enforce, set_authz_enforce

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _mock_request() -> MagicMock:
    request = MagicMock()
    request.scope = {}
    return request


def _oauth_claims() -> OAuthAccessTokenClaims:
    return OAuthAccessTokenClaims(
        client_id="client-1",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        scopes=["trigger:run"],
        token_family="fam",
        token_sequence=1,
    )


def _reset_ctx() -> None:
    for var in (
        ms._ctx_org_id,
        ms._ctx_role,
        ms._ctx_user_id,
        ms._ctx_key_id,
        ms._ctx_auth_token,
        ms._ctx_auth_type,
        ms._ctx_team_id,
        ms._ctx_key_scope,
        ms._ctx_key_grants,
    ):
        var.set(None)
    ms._ctx_node_allowed_tools.set(None)


@pytest.fixture(autouse=True)
def _ctx() -> Any:
    _reset_ctx()
    yield
    _reset_ctx()
    reset_authz_enforce(set_authz_enforce(True))


class TestFinalizeOauthPrincipalTeamBoundary:
    """The per-request OAuth auth leg resolves the client's team_id (FAR-1476)."""

    async def _run(self, client_team_id: uuid.UUID | None) -> None:
        claims = _oauth_claims()

        async def call_next(request: Any) -> MagicMock:
            return MagicMock(status_code=200)

        with (
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="operator")),
            patch.object(ms, "get_oauth_client_team_id", new=AsyncMock(return_value=client_team_id)),
            patch.object(ms, "clamp_oauth_role", new=MagicMock(return_value="operator")),
            patch.object(ms, "_set_authz_enforce", new=AsyncMock()),
        ):
            await _finalize_oauth_principal(_mock_request(), _API_KEY, claims, call_next)

    async def test_team_bound_client_sets_ctx_team_id(self) -> None:
        await self._run(_TEAM_A)
        assert ms._ctx_team_id.get() == _TEAM_A

    async def test_org_wide_client_sets_ctx_team_id_none(self) -> None:
        await self._run(None)
        assert ms._ctx_team_id.get() is None

    async def test_reads_client_team_in_existing_session(self) -> None:
        """The client-team lookup piggybacks on the session the leg already opens."""
        claims = _oauth_claims()

        async def call_next(request: Any) -> MagicMock:
            return MagicMock(status_code=200)

        session = _mock_session()
        with (
            patch.object(ms, "_session", return_value=_make_session_context(session)) as session_factory,
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="operator")),
            patch.object(ms, "get_oauth_client_team_id", new=AsyncMock(return_value=_TEAM_A)) as team_read,
            patch.object(ms, "clamp_oauth_role", new=MagicMock(return_value="operator")),
            patch.object(ms, "_set_authz_enforce", new=AsyncMock()),
        ):
            await _finalize_oauth_principal(_mock_request(), _API_KEY, claims, call_next)
        session_factory.assert_called_once_with(_ORG_ID)
        team_read.assert_awaited_once_with(session, "client-1")


class TestValidateOauthLiveTeamBoundary:
    """The SSE per-event revalidation leg resolves the client's team_id (FAR-1476)."""

    async def _run(self, client_team_id: uuid.UUID | None) -> bool:
        claims = _oauth_claims()
        session = _mock_session()
        with (
            patch.object(ms, "decode_oauth_access_token", new=MagicMock(return_value=claims)),
            patch.object(ms, "get_settings", return_value=MagicMock(secret_key="k")),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "check_oauth_token_family_valid", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_oauth_client_team_id", new=AsyncMock(return_value=client_team_id)),
            patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value="operator")),
        ):
            return await _validate_oauth_live(_API_KEY)

    async def test_team_bound_client_sets_ctx_team_id(self) -> None:
        assert await self._run(_TEAM_A) is True
        assert ms._ctx_team_id.get() == _TEAM_A

    async def test_org_wide_client_sets_ctx_team_id_none(self) -> None:
        assert await self._run(None) is True
        assert ms._ctx_team_id.get() is None


class TestOauthTeamFeedsExistingGuard:
    """The resolved team feeds the EXISTING ``_team_scoped_key_mismatch`` guard.

    No new per-resource code: once the OAuth leg sets ``_ctx_team_id``, every
    existing team-scoped site (here ``set_pipeline_owners``) rejects a resource
    owned by a different team and permits one owned by the same team — the exact
    mechanism the team-scoped ``mk_`` key tests exercise.
    """

    @pytest.fixture(autouse=True)
    def _auth_ok(self) -> Any:
        """A live operator principal with a team boundary (post-OAuth-leg state)."""
        ms._ctx_org_id.set(_ORG_ID)
        ms._ctx_role.set("operator")
        ms._ctx_user_id.set(_USER_ID)
        ms._ctx_key_id.set(uuid.UUID(int=0))
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_auth_type.set("oauth")
        ms._ctx_key_scope.set("user")
        ms._ctx_key_grants.set(None)
        ms._ctx_node_allowed_tools.set(None)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "check_tool_scope", MagicMock()),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
        ):
            yield

    async def test_team_bound_oauth_token_rejected_for_other_teams_resource(self) -> None:
        """A team-A OAuth token is rejected for a team-B-owned pipeline."""
        ms._ctx_team_id.set(_TEAM_A)
        try:
            with patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_B)):
                result = await ms.set_pipeline_owners(
                    pipeline_id=str(uuid.uuid4()), business_owner_id=None, reliability_owner_id=None
                )
        finally:
            ms._ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"

    async def test_team_bound_oauth_token_permitted_for_own_teams_resource(self) -> None:
        """A team-A OAuth token is permitted for a team-A-owned pipeline."""
        pipeline = MagicMock()
        pipeline.business_owner_id = None
        pipeline.reliability_owner_id = None
        ms._ctx_team_id.set(_TEAM_A)
        try:
            with (
                patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_A)),
                patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(return_value=pipeline)),
            ):
                result = await ms.set_pipeline_owners(
                    pipeline_id=str(uuid.uuid4()), business_owner_id=None, reliability_owner_id=None
                )
        finally:
            ms._ctx_team_id.set(None)
        assert result.get("error") != "team_boundary_violation"

    async def test_org_wide_oauth_token_unconstrained(self) -> None:
        """An org-wide (None-boundary) OAuth token is not team-constrained."""
        pipeline = MagicMock()
        pipeline.business_owner_id = None
        pipeline.reliability_owner_id = None
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_B)),
            patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(return_value=pipeline)),
        ):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(uuid.uuid4()), business_owner_id=None, reliability_owner_id=None
            )
        assert result.get("error") != "team_boundary_violation"
