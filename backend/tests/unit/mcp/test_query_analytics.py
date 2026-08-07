"""Unit tests for the query_analytics MCP tool (FAR-102, Part C).

Covers: auth failure, insufficient scope (the analytics.query permission),
the analytics_page feature gate, typed-param parsing (multi-value pipeline_id,
error_code, invalid enums), and the shared-service success path.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import query_analytics
from modulo.core.mcp.scope_validator import MCPAuthorizationError

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class _AuthContext:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)


class _EnabledPlan:
    def feature_enabled(self, name: str) -> bool:
        return True

    def tier(self) -> str:
        return "community"

    def list_enabled_features(self) -> list:
        return []


class _DisabledPlan:
    def feature_enabled(self, name: str) -> bool:
        return False

    def tier(self) -> str:
        return "community"

    def list_enabled_features(self) -> list:
        return []


def _patch_plan(enabled: bool):
    """Patch the org lookup + plan resolution to return an enabled/disabled plan."""
    plan = _EnabledPlan() if enabled else _DisabledPlan()
    return (
        patch("modulo.api.mcp_server._session", return_value=_make_session_context(AsyncMock())),
        patch(
            "modulo.db.crud.organisation.get_organisation",
            new=AsyncMock(return_value=MagicMock(settings_json={}, plan_id=None)),
        ),
        patch("modulo.core.feature_flags.resolve_plan_context", new=AsyncMock(return_value=plan)),
        patch("modulo.api.mcp_server.get_settings", return_value=MagicMock()),
    )


class TestQueryAnalytics(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await query_analytics()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_insufficient_scope_denies_without_analytics_query(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await query_analytics()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_feature_gate_off_returns_feature_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=False)
        with session_cm, org_patch, plan_patch, settings_patch:
            result = await query_analytics()
        assert result["error"] == "feature_required"
        assert "analytics_page" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_enum_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
        with session_cm, org_patch, plan_patch, settings_patch:
            result = await query_analytics(group_by="fortnight")
        assert result["error"] == "invalid_params"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_pipeline_id_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
        with session_cm, org_patch, plan_patch, settings_patch:
            result = await query_analytics(pipeline_id=["not-a-uuid"])
        assert result["error"] == "invalid_params"
        assert "pipeline_id" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_success_passes_typed_params_to_service(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        canned = {
            "group_by": "day",
            "dimension": "error_code",
            "date_from": "2026-08-01T00:00:00+00:00",
            "date_to": "2026-08-07T23:59:59+00:00",
            "buckets": [{"date": "2026-08-07", "key": "executor_stalled", "count": 1, "stall_count": 1}],
        }
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
        pid_a = uuid.uuid4()
        pid_b = uuid.uuid4()
        with (
            session_cm,
            org_patch,
            plan_patch,
            settings_patch,
            patch(
                "modulo.api.mcp_server._get_session_factory",
                return_value=AsyncMock(),
            ),
            patch(
                "modulo.api.mcp_server.run_analytics_query",
                new=AsyncMock(return_value=canned),
            ) as mock_run,
        ):
            result = await query_analytics(
                dimension="error_code",
                pipeline_id=[str(pid_a), str(pid_b)],
                error_code="executor_stalled",
                group_by="day",
            )
        assert result == canned
        mock_run.assert_awaited_once()
        kwargs = mock_run.await_args.kwargs
        params = kwargs["params"]
        assert kwargs["org_id"] == _PLACEHOLDER_ORG_ID
        assert params.pipeline_ids == (pid_a, pid_b)
        assert params.error_code == "executor_stalled"
        assert params.dimension is not None and params.dimension.value == "error_code"
        assert params.group_by is not None and params.group_by.value == "day"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_rate_limited_maps_to_error_dict(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.analytics.service import AnalyticsRateLimitedError

        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
        with (
            session_cm,
            org_patch,
            plan_patch,
            settings_patch,
            patch(
                "modulo.api.mcp_server._get_session_factory",
                return_value=AsyncMock(),
            ),
            patch(
                "modulo.api.mcp_server.run_analytics_query",
                new=AsyncMock(side_effect=AnalyticsRateLimitedError("Rate limit exceeded")),
            ),
        ):
            result = await query_analytics()
        assert result["error"] == "rate_limited"
