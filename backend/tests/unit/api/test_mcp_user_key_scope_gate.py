"""FAR-620 Phase 1: user-scoped MCP key enforcement at the auth + tool layers.

Covers the credential-scope gate end to end with mocked persistence:

* ``_authenticate_api_key`` stamps ``_ctx_key_scope`` from the key row and
  DENIES stored ``scope='user'`` keys with 401 while the
  ``user_scoped_mcp_keys`` flag is OFF (disabling revokes, never broadens).
* Org-key authentication never consults the flag (byte-identical behaviour).
* The regular-JWT branch carries auth_type ``'jwt'`` + caller scope
  ``'user'``; the OAuth-token path keeps auth_type ``'oauth'`` with caller
  scope ``'user'``.
* ``validate_current_auth`` dispatches the new ``'jwt'`` branch to
  ``_validate_principal_live``; the OAuth revalidation path is untouched.
* The live-role clamp (ADR 017 ceiling) is reused verbatim for user-scoped
  keys: runner key + live viewer degrades; missing membership dies (401).
* Handler-level: ``create_api_key`` is org-only — denied under a user-scoped
  key, unchanged under org keys, and the denial survives the kill switch.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jwt import InvalidTokenError as JWTError

import modulo.api.mcp_server as ms
from modulo.api.mcp_server import (
    _authenticate_api_key,
    _authenticate_oauth_jwt,
    _finalize_oauth_principal,
    _set_user_keys_flag,
    validate_current_auth,
)
from modulo.auth.permissions import reset_authz_enforce, set_authz_enforce
from modulo.core.mcp.scope_validator import MCPAuthorizationError

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
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


def _make_execute_result(scalar_one_or_none: Any = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_one_or_none
    return result


def _mock_factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    factory.return_value = cm
    return factory


def _mock_request() -> MagicMock:
    request = MagicMock()
    request.scope = {}
    return request


def _mock_api_key(
    role: str = "operator",
    *,
    scope: str = "org",
    team_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> MagicMock:
    key = MagicMock()
    key.id = _USER_ID
    key.role = role
    key.scope = scope
    key.organisation_id = _ORG_ID
    key.account_id = _USER_ID
    key.team_id = team_id
    key.run_id = run_id
    key.name = None
    return key


def _reset_ctx() -> None:
    for var in (
        ms._ctx_org_id,
        ms._ctx_role,
        ms._ctx_user_id,
        ms._ctx_key_id,
        ms._ctx_auth_token,
        ms._ctx_auth_type,
        ms._ctx_team_id,
    ):
        var.set(None)
    ms._ctx_key_scope.set(None)
    ms._ctx_user_keys_enabled.set(False)
    ms._ctx_node_allowed_tools.set(None)


@pytest.fixture(autouse=True)
def _ctx() -> Any:
    _reset_ctx()
    yield
    _reset_ctx()
    reset_authz_enforce(set_authz_enforce(True))


_API_KEY_AUTH_PATCHES = (
    "modulo.db.rls._ensure_active_transaction",
    "modulo.api.mcp_server._node_allowed_tools_for_key",
)


def _api_key_patches(key: MagicMock, live_role: str | None) -> list[Any]:
    """The standard seam patches for ``_authenticate_api_key`` (postgres path)."""
    session = _mock_session()
    session.execute.return_value = _make_execute_result(scalar_one_or_none=_ORG_ID)
    return [
        patch("modulo.db.rls._ensure_active_transaction", new=AsyncMock(return_value="postgresql")),
        patch("modulo.api.mcp_server._node_allowed_tools_for_key", new=AsyncMock(return_value=None)),
        patch.object(ms, "_get_session_factory", return_value=_mock_factory(session)),
        patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
        patch.object(ms, "validate_api_key", new=AsyncMock(return_value=key)),
        patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value=live_role)),
    ]


class TestApiKeyAuthScopeStamping:
    @pytest.mark.asyncio
    async def test_org_key_sets_key_scope_org(self) -> None:
        patches = _api_key_patches(_mock_api_key(scope="org"), "operator")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is True
        assert err is None
        assert ms._ctx_key_scope.get() == "org"
        assert ms._ctx_auth_type.get() == "api_key"
        assert ms._ctx_user_keys_enabled.get() is False

    @pytest.mark.asyncio
    async def test_user_key_flag_on_sets_key_scope_user(self) -> None:
        """Flag ON: the REAL ``_set_user_keys_flag`` resolves via the registry
        and stamps ``_ctx_user_keys_enabled``."""
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(return_value=True)
        patches = _api_key_patches(_mock_api_key(scope="user"), "operator")
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(ms, "get_registry", return_value=registry),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is True
        assert err is None
        assert ms._ctx_key_scope.get() == "user"
        assert ms._ctx_user_keys_enabled.get() is True

    @pytest.mark.asyncio
    async def test_user_key_flag_off_denies_401(self) -> None:
        """Flag OFF ⇒ a stored user-scoped key is DENIED AT AUTH (401) —
        disabling revokes, never broadens. Uses the REAL flag helper with a
        False-resolving registry (the same fail-closed default an empty org
        override produces)."""
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(return_value=False)
        patches = _api_key_patches(_mock_api_key(scope="user"), "operator")
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(ms, "get_registry", return_value=registry),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is False
        assert err is not None
        assert err.status_code == 401

    @pytest.mark.asyncio
    async def test_user_key_flag_read_failure_fails_closed_401(self) -> None:
        """A flag resolution error is treated as OFF (fail-closed)."""
        patches = _api_key_patches(_mock_api_key(scope="user"), "operator")
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(ms, "get_registry", side_effect=RuntimeError("flag read exploded")),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is False
        assert err is not None
        assert err.status_code == 401

    @pytest.mark.asyncio
    async def test_org_key_never_consults_the_flag(self) -> None:
        """Org-key authentication is byte-identical: the flag read is not
        invoked at all for scope='org' credentials."""
        flag_reader = AsyncMock(return_value=True)
        patches = _api_key_patches(_mock_api_key(scope="org"), "operator")
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(ms, "_set_user_keys_flag", new=flag_reader),
        ):
            handled, _err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is True
        flag_reader.assert_not_awaited()


class TestUserKeyLiveRoleClamp:
    """The ADR-017 ceiling is REUSED for user-scoped keys (3d): clamp results
    are independent of key_scope; the degrade and death cells hold."""

    @pytest.mark.asyncio
    async def test_runner_user_key_live_viewer_degrades_to_viewer(self) -> None:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(return_value=True)
        patches = _api_key_patches(_mock_api_key(role="runner", scope="user"), "viewer")
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(ms, "get_registry", return_value=registry),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is True
        assert err is None
        assert ms._ctx_role.get() == "viewer"
        assert ms._ctx_key_scope.get() == "user"

    @pytest.mark.asyncio
    async def test_runner_user_key_clamp_matches_org_key(self) -> None:
        """The same minted/live pair clamps identically for org- and
        user-scoped keys — the ceiling never depends on the caller scope."""
        results: list[str | None] = []
        for scope in ("org", "user"):
            registry = MagicMock()
            registry.resolve_flag = AsyncMock(return_value=True)
            patches = _api_key_patches(_mock_api_key(role="runner", scope=scope), "viewer")
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patch.object(ms, "get_registry", return_value=registry),
            ):
                handled, _err = await _authenticate_api_key(_mock_request(), _API_KEY)
            assert handled is True
            results.append(ms._ctx_role.get())
        assert results == ["viewer", "viewer"]

    @pytest.mark.asyncio
    async def test_user_key_dead_membership_dies_401(self) -> None:
        """Missing/deactivated membership (live None) ⇒ the key dies with 401
        for a user-scoped key, exactly as for an org key."""
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(return_value=True)
        patches = _api_key_patches(_mock_api_key(role="runner", scope="user"), None)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(ms, "get_registry", return_value=registry),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is False
        assert err is not None
        assert err.status_code == 401


class TestJwtAndOauthPaths:
    @pytest.mark.asyncio
    async def test_regular_jwt_sets_auth_type_jwt_and_scope_user(self) -> None:
        principal = MagicMock()
        principal.organisation_id = _ORG_ID
        principal.account_id = _USER_ID
        principal.org_role = "admin"
        settings = MagicMock(secret_key="k")
        request = _mock_request()
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=principal)),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="runner")),
        ):
            handled, err, claims = await _authenticate_oauth_jwt(request, _API_KEY, settings)
        assert handled is True
        assert err is None
        assert claims is None
        # FAR-620: the regular-JWT branch is identity-bound 'user' with the
        # DISTINCT 'jwt' auth type (previously conflated with 'oauth').
        assert ms._ctx_auth_type.get() == "jwt"
        assert ms._ctx_key_scope.get() == "user"
        assert request.scope["auth_principal"]["type"] == "user"

    @pytest.mark.asyncio
    async def test_oauth_token_path_sets_scope_user_and_keeps_oauth(self) -> None:
        claims = MagicMock()
        claims.organisation_id = _ORG_ID
        claims.account_id = _USER_ID
        claims.scopes = ["trigger:run"]

        async def call_next(request: Any) -> MagicMock:
            return MagicMock(status_code=200)

        with (
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="admin")),
            patch.object(ms, "clamp_oauth_role", new=MagicMock(return_value="admin")),
            patch.object(ms, "_set_authz_enforce", new=AsyncMock()),
        ):
            response = await _finalize_oauth_principal(_mock_request(), _API_KEY, claims, call_next)
        assert response.status_code == 200
        assert ms._ctx_auth_type.get() == "oauth"
        assert ms._ctx_key_scope.get() == "user"


class TestValidateCurrentAuthJwtBranch:
    """The CRITICAL co-change: the 'jwt' auth type routes to the SAME
    live-principal revalidation the OAuth path's JWT fallback performs."""

    @pytest.mark.asyncio
    async def test_jwt_branch_revalidates_principal_live(self) -> None:
        ms._ctx_auth_type.set("jwt")
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_org_id.set(_ORG_ID)
        principal = MagicMock()
        principal.organisation_id = _ORG_ID
        principal.account_id = _USER_ID
        with (
            patch.object(ms, "get_settings", return_value=MagicMock(secret_key="k")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=principal)),
            patch.object(ms, "_validate_principal_live", new=AsyncMock(return_value=True)) as live,
        ):
            assert await validate_current_auth() is True
        live.assert_awaited_once_with(_API_KEY, principal)

    @pytest.mark.asyncio
    async def test_jwt_branch_dead_token_fails_closed(self) -> None:
        ms._ctx_auth_type.set("jwt")
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_org_id.set(_ORG_ID)
        with (
            patch.object(ms, "get_settings", return_value=MagicMock(secret_key="k")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(side_effect=JWTError("revoked"))),
        ):
            assert await validate_current_auth() is False

    @pytest.mark.asyncio
    async def test_oauth_revalidation_path_byte_identical(self) -> None:
        """Characterization: the OAuth-token revalidation path is untouched —
        auth_type 'oauth' still delegates to ``_validate_oauth_live`` and the
        new 'jwt' branch is not consulted."""
        ms._ctx_auth_type.set("oauth")
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_org_id.set(_ORG_ID)
        with (
            patch.object(ms, "_validate_oauth_live", new=AsyncMock(return_value=True)) as oauth_live,
            patch.object(ms, "_validate_principal_live", new=AsyncMock(return_value=True)) as principal_live,
        ):
            assert await validate_current_auth() is True
        oauth_live.assert_awaited_once_with(_API_KEY)
        principal_live.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_auth_type_denies(self) -> None:
        ms._ctx_auth_type.set("unknown_type")
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_org_id.set(_ORG_ID)
        assert await validate_current_auth() is False


class TestHandlerEnforcement:
    """Handler-level cells: ``create_api_key`` (org-only) under each
    credential class, via the real chokepoint (no scope-check stubs)."""

    def _set_credential(self, *, key_scope: str | None, auth_type: str | None, role: str = "admin") -> None:
        ms._ctx_org_id.set(_ORG_ID)
        ms._ctx_role.set(role)
        ms._ctx_user_id.set(_USER_ID)
        ms._ctx_key_id.set(_USER_ID)
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_auth_type.set(auth_type)
        ms._ctx_key_scope.set(key_scope)
        ms._ctx_team_id.set(None)
        ms._ctx_node_allowed_tools.set(None)

    @pytest.mark.asyncio
    async def test_org_key_mints_as_today(self) -> None:
        """Under an org key the MCP ``create_api_key`` tool behaves exactly as
        today (mints an org key) — byte-identical org-key behavior."""
        self._set_credential(key_scope="org", auth_type="api_key")
        session = _mock_session()
        session.get = AsyncMock(return_value=MagicMock(is_break_glass=False))
        key_row = MagicMock()
        key_row.id = uuid.uuid4()
        key_row.name = "CI Key"
        key_row.role = "runner"
        key_row.scope = "org"
        key_row.team_id = None
        key_row.lookup_prefix = "abcd1234"
        key_row.created_at = None
        key_row.expires_at = None
        session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", key_row.id))
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="admin")),
        ):
            result = await ms.create_api_key(name="CI Key", role="runner")
        assert "error" not in result
        assert result["role"] == "runner"
        assert result["lookup_prefix"].startswith("mk_")

    @pytest.mark.asyncio
    async def test_user_key_denied_create_api_key(self) -> None:
        """A user-scoped KEY caller is DENIED the org-only mint tool."""
        self._set_credential(key_scope="user", auth_type="api_key")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await ms.create_api_key(name="x", role="runner")
        assert result == {
            "error": "insufficient_scope",
            "detail": ("Tool 'create_api_key' is org-scoped and cannot be called with a user-scoped API key"),
        }

    @pytest.mark.asyncio
    async def test_user_key_denial_survives_kill_switch_off(self) -> None:
        """The caller-scope leg is kill-switch-INELIGIBLE: with the org's
        authz-enforce kill switch OFF (role leg bypassed) the user-key denial
        still applies (the combined-legs row, through the handler)."""
        self._set_credential(key_scope="user", auth_type="api_key")
        token = set_authz_enforce(False)
        try:
            with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
                result = await ms.create_api_key(name="x", role="runner")
        finally:
            reset_authz_enforce(token)
        assert result["error"] == "insufficient_scope"

    @pytest.mark.asyncio
    async def test_jwt_caller_keeps_org_tool_access(self) -> None:
        """Identity-bound JWT sessions keep today's access to org-only tools
        (only user-scoped KEYS are denied)."""
        self._set_credential(key_scope="user", auth_type="jwt")
        session = _mock_session()
        session.get = AsyncMock(return_value=MagicMock(is_break_glass=False))
        key_row = MagicMock()
        key_row.id = uuid.uuid4()
        session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", key_row.id))
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="admin")),
        ):
            result = await ms.create_api_key(name="From Remy", role="runner")
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_team_scoped_org_key_unaffected(self) -> None:
        """A team-scoped ORG key (scope 'org') passes org-only tools — the
        caller-scope leg is orthogonal to the team boundary."""
        self._set_credential(key_scope="org", auth_type="api_key")
        ms._ctx_team_id.set(uuid.UUID("00000000-0000-0000-0000-000000000010"))
        session = _mock_session()
        session.get = AsyncMock(return_value=MagicMock(is_break_glass=False))
        key_row = MagicMock()
        key_row.id = uuid.uuid4()
        session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", key_row.id))
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="admin")),
        ):
            result = await ms.create_api_key(name="Team Key Mint", role="runner")
        assert "error" not in result


class TestSetUserKeysFlag:
    @pytest.mark.asyncio
    async def test_resolves_true_and_sets_contextvar(self) -> None:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(return_value=True)
        with patch.object(ms, "get_registry", return_value=registry):
            assert await _set_user_keys_flag(_ORG_ID) is True
        registry.resolve_flag.assert_awaited_once_with("user_scoped_mcp_keys", org_id=_ORG_ID)
        assert ms._ctx_user_keys_enabled.get() is True

    @pytest.mark.asyncio
    async def test_read_failure_fails_closed(self) -> None:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(ms, "get_registry", return_value=registry):
            assert await _set_user_keys_flag(_ORG_ID) is False
        assert ms._ctx_user_keys_enabled.get() is False

    @pytest.mark.asyncio
    async def test_resolve_flag_exception_path_returns_false(self) -> None:
        """A registry construction failure is also fail-closed."""
        with patch.object(ms, "get_registry", side_effect=RuntimeError("no registry")):
            assert await _set_user_keys_flag(_ORG_ID) is False
        assert ms._ctx_user_keys_enabled.get() is False


def test_mcp_authorization_error_import_stable() -> None:
    """The handler error-contract exception surfaces through the resolver."""
    assert issubclass(MCPAuthorizationError, Exception)
