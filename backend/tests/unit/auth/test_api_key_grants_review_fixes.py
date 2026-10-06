"""FAR-1477 review fixes: privilege helpers honour grants, live MCP re-validation, fail-closed guards."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api import mcp_server
from modulo.api.dependencies import _assert_tenant_permission, require_in_dev_operator
from modulo.api.routes.pipelines import _is_guardrail_admin, _is_privileged, _may_manage_cost
from modulo.auth import api_key as api_key_mod
from modulo.auth.api_key import (
    ApiKeyGrantsUnavailableError,
    ApiKeyInvalidError,
    api_key_grants_enabled,
    resolve_key_grants,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.mcp.scope_validator import resolve_tool_access


def _principal(role: str, grants: frozenset[str] | None) -> TenantPrincipal:
    return TenantPrincipal(
        username="k",
        organisation_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role=role,
        key_grants=grants,
    )


def _key(grants: object) -> MagicMock:
    key = MagicMock()
    key.id = uuid.uuid4()
    key.organisation_id = uuid.uuid4()
    key.grants = grants
    return key


# ── 1. role-only privilege helpers must also require the matching grant ─────


class TestPipelinePrivilegeHelpers:
    def test_is_privileged_null_grants_unchanged(self) -> None:
        assert _is_privileged("operator", None) is True
        assert _is_privileged("runner", None) is False

    def test_is_privileged_narrow_grants_denied(self) -> None:
        assert _is_privileged("admin", frozenset({"pipeline.list"})) is False

    def test_is_privileged_with_graph_update_grant(self) -> None:
        assert _is_privileged("operator", frozenset({"pipeline.graph.update"})) is True

    def test_is_privileged_empty_grants_denied(self) -> None:
        assert _is_privileged("admin", frozenset()) is False

    def test_guardrail_admin_null_grants_unchanged(self) -> None:
        assert _is_guardrail_admin(_principal("admin", None)) is True
        assert _is_guardrail_admin(_principal("operator", None)) is False

    def test_guardrail_admin_narrow_grants_denied(self) -> None:
        assert _is_guardrail_admin(_principal("admin", frozenset({"pipeline.graph.update"}))) is False

    def test_may_manage_cost_null_grants_unchanged(self) -> None:
        assert _may_manage_cost(_principal("admin", None)) is True
        assert _may_manage_cost(_principal("operator", None)) is False

    def test_may_manage_cost_narrow_grants_denied(self) -> None:
        assert _may_manage_cost(_principal("admin", frozenset({"pipeline.list"}))) is False

    def test_may_manage_cost_granted_and_admin(self) -> None:
        assert _may_manage_cost(_principal("admin", frozenset({"cost.manage"}))) is True

    def test_in_dev_operator_null_grants_unchanged(self) -> None:
        assert require_in_dev_operator(_principal("operator", None), "connector.list.in_dev") is None

    def test_in_dev_operator_narrow_grants_denied(self) -> None:
        with pytest.raises(HTTPException) as exc:
            require_in_dev_operator(_principal("operator", frozenset({"connector.list"})), "connector.list.in_dev")
        assert exc.value.status_code == 403

    def test_in_dev_operator_granted_passes(self) -> None:
        result = require_in_dev_operator(
            _principal("operator", frozenset({"connector.list.in_dev"})),
            "connector.list.in_dev",
        )
        assert result is None


class TestMcpPrivilegeHelpers:
    def test_ctx_may_manage_cost_null_grants_unchanged(self) -> None:
        with patch.object(mcp_server, "_ctx_role_val", return_value="admin"):
            token = mcp_server._ctx_key_grants.set(None)
            try:
                assert mcp_server._ctx_may_manage_cost() is True
            finally:
                mcp_server._ctx_key_grants.reset(token)

    def test_ctx_may_manage_cost_narrow_grants_denied(self) -> None:
        with patch.object(mcp_server, "_ctx_role_val", return_value="admin"):
            token = mcp_server._ctx_key_grants.set(frozenset({"pipeline.list"}))
            try:
                assert mcp_server._ctx_may_manage_cost() is False
            finally:
                mcp_server._ctx_key_grants.reset(token)


# ── 3. pin: MCP reads use the coarse resource.read_only key (open design question) ──


class TestMcpReadOnlyCoarseGrant:
    @staticmethod
    def _allowed(grants: frozenset[str]) -> bool:
        allowed, _ = resolve_tool_access(
            tool="list_pipelines",
            action=None,
            role="operator",
            key_scope="org",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=True,
            grants=grants,
        )
        return allowed

    def test_fine_grained_rest_key_does_not_open_mcp_read_tool(self) -> None:
        assert self._allowed(frozenset({"pipeline.list"})) is False

    def test_coarse_read_only_key_opens_mcp_read_tool(self) -> None:
        assert self._allowed(frozenset({"resource.read_only"})) is True


# ── 4. live MCP re-validation re-runs the grant resolver ────────────────────


class TestValidateApiKeyLive:
    @staticmethod
    def _patches(key: MagicMock, resolver: AsyncMock):
        @asynccontextmanager
        async def _fake_session(_org: uuid.UUID):
            yield MagicMock()

        return (
            patch.object(mcp_server, "_session", _fake_session),
            patch.object(mcp_server, "validate_api_key", AsyncMock(return_value=key)),
            patch.object(mcp_server, "_revalidate_live_role", AsyncMock(return_value="operator")),
            patch.object(mcp_server, "resolve_key_grants", resolver),
        )

    @pytest.mark.asyncio
    async def test_flag_off_kills_grant_bearing_key(self) -> None:
        key = _key("run.trigger")
        key.role = "operator"
        resolver = AsyncMock(side_effect=ApiKeyInvalidError)
        uid = mcp_server._ctx_user_id.set(uuid.uuid4())
        p1, p2, p3, p4 = self._patches(key, resolver)
        try:
            with p1, p2, p3, p4, pytest.raises(ApiKeyInvalidError):
                await mcp_server._validate_api_key_live("mk_x", uuid.uuid4())
        finally:
            mcp_server._ctx_user_id.reset(uid)

    @pytest.mark.asyncio
    async def test_refreshes_cached_grants(self) -> None:
        key = _key("run.trigger")
        key.role = "operator"
        resolver = AsyncMock(return_value=frozenset({"run.trigger"}))
        uid = mcp_server._ctx_user_id.set(uuid.uuid4())
        gtok = mcp_server._ctx_key_grants.set(frozenset({"stale.grant"}))
        p1, p2, p3, p4 = self._patches(key, resolver)
        try:
            with p1, p2, p3, p4:
                assert await mcp_server._validate_api_key_live("mk_x", uuid.uuid4()) is True
            assert mcp_server._ctx_key_grants.get(None) == frozenset({"run.trigger"})
        finally:
            mcp_server._ctx_key_grants.reset(gtok)
            mcp_server._ctx_user_id.reset(uid)

    @pytest.mark.asyncio
    async def test_flag_off_means_validate_current_auth_denies(self) -> None:
        with patch.object(mcp_server, "_validate_api_key_live", AsyncMock(side_effect=ApiKeyInvalidError)):
            atok = mcp_server._ctx_auth_type.set("api_key")
            try:
                assert await mcp_server.validate_current_auth() is False
            finally:
                mcp_server._ctx_auth_type.reset(atok)


# ── 5. flag-read failure: 503-style unavailable, only for grant-bearing keys ──


class TestFlagReadFailure:
    @staticmethod
    def _failing_registry() -> MagicMock:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(side_effect=RuntimeError("boom"))
        return registry

    @pytest.mark.asyncio
    async def test_strict_read_raises_unavailable(self) -> None:
        with (
            patch.object(api_key_mod, "get_registry", return_value=self._failing_registry()),
            pytest.raises(ApiKeyGrantsUnavailableError),
        ):
            await api_key_grants_enabled(uuid.uuid4(), strict=True)

    @pytest.mark.asyncio
    async def test_non_strict_read_still_fails_closed_false(self) -> None:
        with patch.object(api_key_mod, "get_registry", return_value=self._failing_registry()):
            assert await api_key_grants_enabled(uuid.uuid4()) is False

    @pytest.mark.asyncio
    async def test_grant_bearing_key_gets_unavailable_not_invalid(self) -> None:
        with (
            patch.object(api_key_mod, "get_registry", return_value=self._failing_registry()),
            pytest.raises(ApiKeyGrantsUnavailableError) as exc,
        ):
            await resolve_key_grants(_key("run.trigger"))
        assert not isinstance(exc.value, ApiKeyInvalidError)

    @pytest.mark.asyncio
    async def test_null_grants_key_never_touches_flag_even_when_broken(self) -> None:
        registry = self._failing_registry()
        with patch.object(api_key_mod, "get_registry", return_value=registry):
            assert await resolve_key_grants(_key(None)) is None
        registry.resolve_flag.assert_not_called()


# ── 6. malformed values fail closed ─────────────────────────────────────────


class TestMalformedValuesFailClosed:
    @pytest.mark.asyncio
    async def test_non_string_grants_column_denied(self) -> None:
        with pytest.raises(ApiKeyInvalidError):
            await resolve_key_grants(_key(["run.trigger"]))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [["run.trigger"], {"run.trigger"}, "run.trigger", 5])
    async def test_non_frozenset_principal_grants_denied(self, bad: object) -> None:
        principal = _principal("runner", None)
        object.__setattr__(principal, "key_grants", bad)
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=cm)
        with (
            patch("modulo.api.dependencies.resolve_authz_enforce", AsyncMock(return_value=True)),
            pytest.raises(HTTPException) as exc,
        ):
            await _assert_tenant_permission(session, principal, "run.trigger", "runner")
        assert exc.value.status_code == 403
