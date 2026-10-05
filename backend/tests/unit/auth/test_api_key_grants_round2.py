"""FAR-1477 second-round review fixes: grants carried into the service layer, strict flag read."""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api import mcp_server
from modulo.api.dependencies import require_in_dev_operator
from modulo.api.routes.pipelines import (
    _grants_deny_guardrail_admin,
    _grants_deny_privilege,
    _is_guardrail_admin,
    _is_privileged,
)
from modulo.auth import api_key as api_key_mod
from modulo.auth.api_key import ApiKeyGrantsUnavailableError, api_key_grants_enabled
from modulo.auth.jwt import TenantPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.db.crud.hitl_review_guard import (
    GuardrailBindingStripDenied,
    enforce_guardrail_binding_strip,
    resolve_effective_privilege,
)

_ROLE_PATH = "modulo.db.crud.org_membership.resolve_role_from_membership"
_ROWS_PATH = "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows"
_ENGINE_PATH = "modulo.api.dependencies.get_or_create_engine"


def _principal(role: str, grants: frozenset[str] | None) -> TenantPrincipal:
    return TenantPrincipal(
        username="k",
        organisation_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role=role,
        key_grants=grants,
    )


class TestPrivilegeCarriesGrantDecision:
    """Live role stays authoritative; the grants denial can only narrow it."""

    @pytest.mark.asyncio
    async def test_grant_restricted_admin_key_is_not_privileged(self) -> None:
        principal = _principal("admin", frozenset({"pipeline.list"}))
        route_flag = _is_privileged(principal.org_role, principal.key_grants)
        with patch(_ROLE_PATH, AsyncMock(return_value="admin")):
            effective = await resolve_effective_privilege(
                AsyncMock(),
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                is_privileged=route_flag,
                caller_type="rest",
                grants_deny_privilege=_grants_deny_privilege(principal),
            )
        assert effective is False

    @pytest.mark.asyncio
    async def test_null_grants_admin_key_unchanged(self) -> None:
        principal = _principal("admin", None)
        route_flag = _is_privileged(principal.org_role, principal.key_grants)
        with patch(_ROLE_PATH, AsyncMock(return_value="admin")):
            effective = await resolve_effective_privilege(
                AsyncMock(),
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                is_privileged=route_flag,
                caller_type="rest",
                grants_deny_privilege=_grants_deny_privilege(principal),
            )
        assert effective is True

    @pytest.mark.asyncio
    async def test_onboarding_style_default_flag_gets_live_role_upgrade(self) -> None:
        # onboarding passes a default/stale-low flag with an admin account: the
        # live role must still upgrade it (origin/main semantics).
        with patch(_ROLE_PATH, AsyncMock(return_value="admin")):
            effective = await resolve_effective_privilege(
                AsyncMock(),
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                is_privileged=False,
                caller_type="rest",
            )
        assert effective is True

    @pytest.mark.asyncio
    async def test_grants_deny_beats_live_admin_role(self) -> None:
        with patch(_ROLE_PATH, AsyncMock(return_value="admin")):
            effective = await resolve_effective_privilege(
                AsyncMock(),
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                is_privileged=True,
                caller_type="rest",
                grants_deny_privilege=True,
            )
        assert effective is False

    @pytest.mark.asyncio
    async def test_live_role_still_narrows(self) -> None:
        with patch(_ROLE_PATH, AsyncMock(return_value="runner")):
            effective = await resolve_effective_privilege(
                AsyncMock(),
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                is_privileged=True,
                caller_type="rest",
            )
        assert effective is False


class TestGuardrailStripCarriesGrantDecision:
    @staticmethod
    def _bound_row(node_id: str) -> MagicMock:
        row = MagicMock()
        row.node_id = node_id
        return row

    @pytest.mark.asyncio
    async def test_grant_restricted_admin_key_cannot_strip(self) -> None:
        principal = _principal("admin", frozenset({"pipeline.graph.update"}))
        route_flag = _is_guardrail_admin(principal)
        with (
            patch(_ROLE_PATH, AsyncMock(return_value="admin")),
            patch(_ROWS_PATH, AsyncMock(return_value=[self._bound_row("n1")])),
            pytest.raises(GuardrailBindingStripDenied),
        ):
            await enforce_guardrail_binding_strip(
                AsyncMock(),
                pipeline_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                incoming_node_ids=set(),
                is_guardrail_admin=route_flag,
                caller_type="rest",
                account_id=uuid.uuid4(),
                grants_deny_guardrail_admin=_grants_deny_guardrail_admin(principal),
            )

    @pytest.mark.asyncio
    async def test_onboarding_style_default_flag_gets_live_admin(self) -> None:
        rows = AsyncMock(return_value=[self._bound_row("n1")])
        with patch(_ROLE_PATH, AsyncMock(return_value="admin")), patch(_ROWS_PATH, rows):
            await enforce_guardrail_binding_strip(
                AsyncMock(),
                pipeline_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                incoming_node_ids=set(),
                is_guardrail_admin=False,
                caller_type="rest",
                account_id=uuid.uuid4(),
            )
        rows.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_null_grants_admin_key_may_strip(self) -> None:
        principal = _principal("admin", None)
        route_flag = _is_guardrail_admin(principal)
        rows = AsyncMock(return_value=[self._bound_row("n1")])
        with patch(_ROLE_PATH, AsyncMock(return_value="admin")), patch(_ROWS_PATH, rows):
            await enforce_guardrail_binding_strip(
                AsyncMock(),
                pipeline_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                incoming_node_ids=set(),
                is_guardrail_admin=route_flag,
                caller_type="rest",
                account_id=uuid.uuid4(),
                grants_deny_guardrail_admin=_grants_deny_guardrail_admin(principal),
            )
        rows.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_demoted_owner_still_denied_despite_admin_flag(self) -> None:
        with (
            patch(_ROLE_PATH, AsyncMock(return_value="operator")),
            patch(_ROWS_PATH, AsyncMock(return_value=[self._bound_row("n1")])),
            pytest.raises(GuardrailBindingStripDenied),
        ):
            await enforce_guardrail_binding_strip(
                AsyncMock(),
                pipeline_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                incoming_node_ids=set(),
                is_guardrail_admin=True,
                caller_type="rest",
                account_id=uuid.uuid4(),
            )


class TestStrictFlagReadThroughRealRegistry:
    """Exercises FeatureFlagRegistry.resolve_flag itself, with a failing override read."""

    @staticmethod
    def _registry() -> FeatureFlagRegistry:
        return FeatureFlagRegistry(current_tier="community")

    @pytest.mark.asyncio
    async def test_strict_raises_unavailable_on_override_read_failure(self) -> None:
        with (
            patch.object(api_key_mod, "get_registry", return_value=self._registry()),
            patch(_ENGINE_PATH, side_effect=RuntimeError("db down")),
            pytest.raises(ApiKeyGrantsUnavailableError),
        ):
            await api_key_grants_enabled(uuid.uuid4(), strict=True)

    @pytest.mark.asyncio
    async def test_non_strict_still_off_on_override_read_failure(self) -> None:
        with (
            patch.object(api_key_mod, "get_registry", return_value=self._registry()),
            patch(_ENGINE_PATH, side_effect=RuntimeError("db down")),
        ):
            assert await api_key_grants_enabled(uuid.uuid4()) is False

    @pytest.mark.asyncio
    async def test_default_registry_callers_still_swallow_error(self) -> None:
        with patch(_ENGINE_PATH, side_effect=RuntimeError("db down")):
            assert await self._registry().resolve_flag("api_key_grants", org_id=uuid.uuid4()) is False


class TestValidateCurrentAuthGrantsUnavailable:
    @pytest.mark.asyncio
    async def test_retryable_denial_without_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        atok = mcp_server._ctx_auth_type.set("api_key")
        ttok = mcp_server._ctx_auth_token.set("mk_x")
        otok = mcp_server._ctx_org_id.set(uuid.uuid4())
        try:
            with (
                caplog.at_level(logging.DEBUG),
                patch.object(
                    mcp_server,
                    "_validate_api_key_live",
                    AsyncMock(side_effect=ApiKeyGrantsUnavailableError),
                ),
            ):
                assert await mcp_server.validate_current_auth() is False
        finally:
            mcp_server._ctx_org_id.reset(otok)
            mcp_server._ctx_auth_token.reset(ttok)
            mcp_server._ctx_auth_type.reset(atok)
        records = [r for r in caplog.records if "grants_unavailable" in r.getMessage()]
        assert [r.levelno for r in records] == [logging.WARNING]
        assert [r for r in caplog.records if r.exc_info] == []


class TestInDevOperatorGrantDenialDetail:
    def test_grant_denial_names_the_key_not_the_role(self) -> None:
        with pytest.raises(HTTPException) as exc:
            require_in_dev_operator(_principal("admin", frozenset({"connector.list"})), "connector.list.in_dev")
        assert exc.value.status_code == 403
        assert "not granted to this API key" in exc.value.detail

    def test_role_denial_keeps_role_message(self) -> None:
        with pytest.raises(HTTPException) as exc:
            require_in_dev_operator(_principal("viewer", None), "connector.list.in_dev")
        assert "requires" in exc.value.detail


class TestMintStrictFlagRead:
    """The mint path must not skip the 90-day cap / grants gate on a flag-read failure."""

    @staticmethod
    async def _mint(grants: list[str] | None, scope: str) -> None:
        from modulo.api.routes import api_keys as routes

        req = MagicMock()
        req.name = "k"
        req.grants = grants
        req.team_id = None
        req.expires_at = None
        with (
            patch.object(routes, "_validate_create_request", AsyncMock(return_value=scope)),
            patch.object(routes, "_resolve_new_team_id", AsyncMock(return_value=None)),
            patch.object(routes, "api_key_grants_enabled", AsyncMock(side_effect=ApiKeyGrantsUnavailableError)),
        ):
            await routes.create_api_key_endpoint(
                req=req,
                session=AsyncMock(),
                principal=_principal("admin", None),
                settings=MagicMock(),
            )

    @pytest.mark.asyncio
    async def test_user_scope_mint_503_when_flag_unreadable(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._mint(None, "user")
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_grant_bearing_mint_503_when_flag_unreadable(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._mint(["run.trigger"], "org")
        assert exc.value.status_code == 503
