"""FAR-620 stage 2: the FAR-614 caller-scoped MCP tools (``hitl_email.self``).

``get_hitl_email_alerts`` / ``set_hitl_email_alerts`` operate on the CALLER'S
OWN HITL email-alert preference — there is NO target parameter (the stage-1
registry-introspection contract). Under an org-wide or run-scoped key the
caller-scope classification denies both tools with the pinned
``{"error": "insufficient_scope", ...}`` shape (visible-but-failing in
tools/list); JWT/OAuth callers and user-scoped keys are allowed; JWT/Remy
callers are INCLUDING (documented trust level = the JWT-only REST UI).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.api.mcp_server as ms
from modulo.api.mcp_server import get_hitl_email_alerts, set_hitl_email_alerts

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_OTHER_PIPELINE = uuid.UUID("00000000-0000-0000-0000-00000000000b")


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


def _set_credential(*, key_scope: str | None, auth_type: str | None, role: str = "viewer") -> None:
    ms._ctx_org_id.set(_ORG_ID)
    ms._ctx_role.set(role)
    ms._ctx_user_id.set(_USER_ID)
    ms._ctx_key_id.set(_USER_ID)
    ms._ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
    ms._ctx_auth_type.set(auth_type)
    ms._ctx_key_scope.set(key_scope)
    ms._ctx_team_id.set(None)
    ms._ctx_node_allowed_tools.set(None)


@pytest.fixture(autouse=True)
def _ctx() -> Any:
    from modulo.auth.permissions import reset_authz_enforce, set_authz_enforce

    _set_credential(key_scope=None, auth_type=None)
    ms._ctx_user_keys_enabled.set(False)
    yield
    for attr in (
        "_ctx_org_id",
        "_ctx_role",
        "_ctx_user_id",
        "_ctx_key_id",
        "_ctx_auth_token",
        "_ctx_auth_type",
        "_ctx_team_id",
    ):
        getattr(ms, attr).set(None)
    ms._ctx_key_scope.set(None)
    ms._ctx_user_keys_enabled.set(False)
    ms._ctx_node_allowed_tools.set(None)
    reset_authz_enforce(set_authz_enforce(True))


class TestCallerScopeGate:
    """Org keys + run-scoped keys Denied; JWT (Remy) callers allowed."""

    @pytest.mark.asyncio
    async def test_org_key_denied_get(self) -> None:
        _set_credential(key_scope="org", auth_type="api_key")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await get_hitl_email_alerts()
        assert result == {
            "error": "insufficient_scope",
            "detail": (
                "Tool 'get_hitl_email_alerts' is caller-scoped and requires a "
                "user-scoped credential; this caller's key scope is 'org'"
            ),
        }

    @pytest.mark.asyncio
    async def test_org_key_denied_set(self) -> None:
        """An org-level service key must NEVER alter user-level configuration."""
        _set_credential(key_scope="org", auth_type="api_key")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await set_hitl_email_alerts(enabled=True, pipeline_ids=[str(_PIPELINE_ID)])
        assert result["error"] == "insufficient_scope"
        assert result["detail"].startswith("Tool 'set_hitl_email_alerts' is caller-scoped")

    @pytest.mark.asyncio
    async def test_run_scoped_key_denied(self) -> None:
        """A run-scoped sandbox key carries key_scope 'org' — denied the same
        way as any org-scoped key (fail-closed, kill-switch-ineligible)."""
        _set_credential(key_scope="org", auth_type="api_key", role="runner")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await get_hitl_email_alerts()
            set_result = await set_hitl_email_alerts(enabled=False)
        assert result["error"] == "insufficient_scope"
        assert set_result["error"] == "insufficient_scope"

    @pytest.mark.asyncio
    async def test_org_key_denial_survives_kill_switch_off(self) -> None:
        """The caller-scope leg is kill-switch-INELIGIBLE — with the org's
        authz-enforce kill switch OFF (only the role leg is bypassed) the
        denial still applies."""
        from modulo.auth.permissions import reset_authz_enforce, set_authz_enforce

        _set_credential(key_scope="org", auth_type="api_key")
        token = set_authz_enforce(False)
        try:
            with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
                result = await get_hitl_email_alerts()
        finally:
            reset_authz_enforce(token)
        assert result["error"] == "insufficient_scope"

    @pytest.mark.asyncio
    async def test_jwt_remy_caller_allowed(self) -> None:
        """Identity-bound JWT (Remy) sessions are INCLUDED — the documented
        trust level equals the JWT-only REST UI."""
        _set_credential(key_scope="user", auth_type="jwt", role="viewer")
        session = _mock_session()
        account = MagicMock()
        account.preferences = {"hitl_email": {"default": True, "pipeline_overrides": {}}}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=account)),
        ):
            result = await get_hitl_email_alerts()
        assert result == {"default": True, "pipeline_overrides": {}}

    @pytest.mark.asyncio
    async def test_user_scoped_key_allowed(self) -> None:
        _set_credential(key_scope="user", auth_type="api_key", role="runner")
        session = _mock_session()
        merged = {"hitl_email": {"default": False, "pipeline_overrides": {str(_PIPELINE_ID): True}}}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch(
                "modulo.db.crud.account.set_hitl_email_preference",
                new=AsyncMock(return_value=merged),
            ) as helper,
        ):
            result = await set_hitl_email_alerts(enabled=False, pipeline_ids=[str(_PIPELINE_ID)])
        assert result == {"default": False, "pipeline_overrides": {str(_PIPELINE_ID): True}}
        helper.assert_awaited_once_with(
            session,
            _USER_ID,
            default=False,
            pipeline_overrides={str(_PIPELINE_ID): True},
        )

    @pytest.mark.asyncio
    async def test_unresolved_target_fails_closed(self) -> None:
        """key_scope unset (None) — the caller-scope leg fails closed."""
        _set_credential(key_scope=None, auth_type="jwt")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await get_hitl_email_alerts()
        assert result["error"] == "insufficient_scope"


class TestGetHitlEmailAlerts:
    @pytest.mark.asyncio
    async def test_absent_preferences_return_all_off(self) -> None:
        _set_credential(key_scope="user", auth_type="jwt")
        session = _mock_session()
        account = MagicMock()
        account.preferences = {"theme": "dark"}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=account)),
        ):
            result = await get_hitl_email_alerts()
        assert result == {"default": False, "pipeline_overrides": {}}

    @pytest.mark.asyncio
    async def test_malformed_preferences_normalised(self) -> None:
        _set_credential(key_scope="user", auth_type="jwt")
        session = _mock_session()
        account = MagicMock()
        account.preferences = {"hitl_email": {"default": "yes", "pipeline_overrides": "bad"}}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=account)),
        ):
            result = await get_hitl_email_alerts()
        assert result == {"default": False, "pipeline_overrides": {}}

    @pytest.mark.asyncio
    async def test_account_missing_returns_error_dict(self) -> None:
        _set_credential(key_scope="user", auth_type="jwt")
        session = _mock_session()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=None)),
        ):
            result = await get_hitl_email_alerts()
        assert result["error"] == "account_not_found"


class TestSetHitlEmailAlerts:
    @pytest.mark.asyncio
    async def test_writes_default_through_the_shared_helper(self) -> None:
        _set_credential(key_scope="user", auth_type="oauth")
        session = _mock_session()
        merged = {"hitl_email": {"default": True, "pipeline_overrides": {}}}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch(
                "modulo.db.crud.account.set_hitl_email_preference",
                new=AsyncMock(return_value=merged),
            ) as helper,
        ):
            result = await set_hitl_email_alerts(enabled=True)
        assert result == {"default": True, "pipeline_overrides": {}}
        # pipeline_ids omitted ⇒ overrides untouched (None → helper preserves).
        helper.assert_awaited_once_with(session, _USER_ID, default=True, pipeline_overrides=None)

    @pytest.mark.asyncio
    async def test_pipeline_ids_atomically_replace_overrides(self) -> None:
        """Existing {p1: true} + pipeline_ids=[p2] → {p2: true} ONLY — the
        override list is replaced as a unit, never per-key patched."""
        _set_credential(key_scope="user", auth_type="jwt")
        session = _mock_session()
        merged = {"hitl_email": {"default": True, "pipeline_overrides": {str(_OTHER_PIPELINE): True}}}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch(
                "modulo.db.crud.account.set_hitl_email_preference",
                new=AsyncMock(return_value=merged),
            ) as helper,
        ):
            result = await set_hitl_email_alerts(enabled=True, pipeline_ids=[str(_OTHER_PIPELINE)])
        assert result == {"default": True, "pipeline_overrides": {str(_OTHER_PIPELINE): True}}
        helper.assert_awaited_once_with(
            session, _USER_ID, default=True, pipeline_overrides={str(_OTHER_PIPELINE): True}
        )

    @pytest.mark.asyncio
    async def test_empty_pipeline_ids_clears_overrides_but_omitted_preserves(self) -> None:
        """R9 pin: an EXPLICIT empty list REPLACES the override map with {}
        (all overrides cleared); OMITTING the parameter passes None down so the
        helper preserves the stored map. One test pins both sides of the
        distinction — `[]` and absent are semantically different."""
        _set_credential(key_scope="user", auth_type="jwt")

        # Explicit []: parsed_ids == [] (truthy check is `is not None`) -> the
        # helper receives pipeline_overrides={} -> atomic REPLACE with an
        # empty map = every override cleared.
        cleared = {"hitl_email": {"default": False, "pipeline_overrides": {}}}
        session_clear = _mock_session()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session_clear)),
            patch(
                "modulo.db.crud.account.set_hitl_email_preference",
                new=AsyncMock(return_value=cleared),
            ) as helper,
        ):
            result = await set_hitl_email_alerts(enabled=False, pipeline_ids=[])
        assert result == {"default": False, "pipeline_overrides": {}}
        helper.assert_awaited_once_with(session_clear, _USER_ID, default=False, pipeline_overrides={})

        # Omitted: pipeline_overrides=None -> the helper preserves the stored
        # override map untouched.
        preserved = {"hitl_email": {"default": False, "pipeline_overrides": {str(_PIPELINE_ID): True}}}
        session_keep = _mock_session()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session_keep)),
            patch(
                "modulo.db.crud.account.set_hitl_email_preference",
                new=AsyncMock(return_value=preserved),
            ) as helper,
        ):
            result = await set_hitl_email_alerts(enabled=False)
        assert result == {"default": False, "pipeline_overrides": {str(_PIPELINE_ID): True}}
        helper.assert_awaited_once_with(session_keep, _USER_ID, default=False, pipeline_overrides=None)

    @pytest.mark.asyncio
    async def test_rejects_non_bool_enabled(self) -> None:
        _set_credential(key_scope="user", auth_type="jwt")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await set_hitl_email_alerts(enabled=1)  # type: ignore[arg-type]
        assert result == {
            "error": "invalid_param",
            "field": "enabled",
            "detail": "enabled must be a strict boolean (true/false)",
        }

    @pytest.mark.asyncio
    async def test_rejects_non_uuid_pipeline_id(self) -> None:
        _set_credential(key_scope="user", auth_type="jwt")
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await set_hitl_email_alerts(enabled=True, pipeline_ids=["not-a-uuid"])
        assert result == {
            "error": "invalid_id",
            "field": "pipeline_ids",
            "detail": "Invalid UUID format in pipeline_ids: not-a-uuid",
        }


class TestRegistryIntrospection:
    """Pinned allowed-params map for the NEW tools only (registry
    introspection, FAR-620): the actual FastMCP JSON-schema parameters —
    including the no-target-param invariant — are asserted below."""

    def _params(self, tool: str) -> dict[str, Any]:
        from modulo.api.mcp_server import mcp

        return dict(mcp._tool_manager._tools[tool].parameters)  # type: ignore[attr-defined]

    def test_get_hitl_email_alerts_has_no_params(self) -> None:
        params = self._params("get_hitl_email_alerts")
        assert not params.get("properties")
        assert "required" not in params
        assert params.get("type") == "object"

    def test_set_hitl_email_alerts_allowed_params_pinned(self) -> None:
        params = self._params("set_hitl_email_alerts")
        assert params.get("properties") == {
            "enabled": {"title": "Enabled", "type": "boolean"},
            "pipeline_ids": {
                "anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}],
                "default": None,
                "title": "Pipeline Ids",
            },
        }
        assert params.get("required") == ["enabled"]
        assert params.get("type") == "object"

    def test_no_target_parameter_on_either_tool(self) -> None:
        for tool in ("get_hitl_email_alerts", "set_hitl_email_alerts"):
            properties = self._params(tool).get("properties", {})
            assert "account_id" not in properties
            assert "target" not in properties
            assert "user_id" not in properties
