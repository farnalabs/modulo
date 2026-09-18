"""Unit tests for the query_analytics_concurrency MCP tool (FAR-134).

Covers: auth failure, insufficient scope (the analytics.query permission), the
analytics_page feature gate, typed-param parsing (multi-value pipeline_id,
invalid enums), the shared-service success path, and every service-error
mapping (rate limit, validation, timeout, migration, DB, generic). Mirrors the
query_analytics tool tests — both funnel through the same shared service.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import query_analytics_concurrency
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


def _plan_enabled_patches() -> list[patch]:
    """Plan-enabled patches up to (but not including) the service call."""
    session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
    return [
        session_cm,
        org_patch,
        plan_patch,
        settings_patch,
        patch("modulo.api.mcp_server._get_session_factory", return_value=AsyncMock()),
    ]


class TestQueryAnalyticsConcurrency(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await query_analytics_concurrency()
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
            result = await query_analytics_concurrency()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_feature_gate_off_returns_feature_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=False)
        with session_cm, org_patch, plan_patch, settings_patch:
            result = await query_analytics_concurrency()
        assert result["error"] == "feature_required"
        assert "analytics_page" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_enum_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
        with session_cm, org_patch, plan_patch, settings_patch:
            result = await query_analytics_concurrency(group_by="fortnight")
        assert result["error"] == "invalid_params"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_pipeline_id_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session_cm, org_patch, plan_patch, settings_patch = _patch_plan(enabled=True)
        with session_cm, org_patch, plan_patch, settings_patch:
            result = await query_analytics_concurrency(pipeline_id=["not-a-uuid"])
        assert result["error"] == "invalid_params"
        assert "pipeline_id" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_folder_id_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        with contextlib.ExitStack() as stack:
            for p in _plan_enabled_patches():
                stack.enter_context(p)
            result = await query_analytics_concurrency(folder_id="not-a-uuid")
        assert result["error"] == "invalid_params"
        assert "folder_id" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_success_passes_typed_params_to_service(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        canned = {
            "group_by": "day",
            "date_from": "2026-08-01T00:00:00+00:00",
            "date_to": "2026-08-07T23:59:59+00:00",
            "pool_reference": 20,
            "buckets": [{"date": "2026-08-07", "max_active": 3, "avg_active": 1.5, "max_queued": 2, "avg_queued": 0.5}],
        }
        import contextlib

        pid_a = uuid.uuid4()
        pid_b = uuid.uuid4()
        patches = _plan_enabled_patches()
        patches.append(patch("modulo.api.mcp_server.run_concurrency_query", new=AsyncMock(return_value=canned)))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency(
                pipeline_id=[str(pid_a), str(pid_b)],
                trigger_type="manual",
                status="failed",
                group_by="day",
            )
        assert result == canned
        mock_run = patches[-1].new
        mock_run.assert_awaited_once()
        kwargs = mock_run.await_args.kwargs
        params = kwargs["params"]
        assert kwargs["org_id"] == _PLACEHOLDER_ORG_ID
        assert params.pipeline_ids == (pid_a, pid_b)
        assert params.trigger_type is not None
        assert params.trigger_type.value == "manual"
        assert params.status is not None
        assert params.status.value == "failed"
        assert params.dimension is None, "concurrency has no dimension split"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_limit_is_clamped_to_query_cap(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        canned = {"group_by": "day", "buckets": []}
        patches = _plan_enabled_patches()
        patches.append(patch("modulo.api.mcp_server.run_concurrency_query", new=AsyncMock(return_value=canned)))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency(limit=5000)
        assert result == canned
        mock_run = patches[-1].new
        assert mock_run.await_args.kwargs["params"].limit == 1000, "limit must be clamped to the REST cap of 1000"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_date_params_parse_to_aware_datetimes(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        canned = {"group_by": "day", "date_from": None, "date_to": None, "pool_reference": None, "buckets": []}
        patches = _plan_enabled_patches()
        patches.append(patch("modulo.api.mcp_server.run_concurrency_query", new=AsyncMock(return_value=canned)))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency(date_from="2026-08-01", date_to="2026-08-05T14:00:00Z")
        assert result == canned
        mock_run = patches[-1].new
        params = mock_run.await_args.kwargs["params"]
        assert params.date_from is not None
        assert params.date_from.day == 1
        assert params.date_to is not None
        assert params.date_to.tzinfo is not None, "an ISO datetime keeps its offset"
        assert params.date_to.hour == 14, "an ISO datetime must be parsed, not treated as a bare date"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_rate_limited_maps_to_error_dict(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        from modulo.core.analytics.service import AnalyticsRateLimitedError

        patches = _plan_enabled_patches()
        patches.append(
            patch(
                "modulo.api.mcp_server.run_concurrency_query",
                new=AsyncMock(side_effect=AnalyticsRateLimitedError("Rate limit exceeded")),
            )
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "rate_limited"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_validation_error_maps_to_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        from modulo.core.analytics.service import AnalyticsValidationError

        patches = _plan_enabled_patches()
        patches.append(
            patch(
                "modulo.api.mcp_server.run_concurrency_query",
                new=AsyncMock(side_effect=AnalyticsValidationError("date range must be 365 days or less")),
            )
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "invalid_params"
        assert "365 days" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_query_timeout_maps_to_query_timeout(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        from modulo.core.analytics.service import AnalyticsQueryTimeoutError

        patches = _plan_enabled_patches()
        patches.append(
            patch(
                "modulo.api.mcp_server.run_concurrency_query",
                new=AsyncMock(side_effect=AnalyticsQueryTimeoutError("query exceeded timeout")),
            )
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "query_timeout"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_migration_required_maps_to_migration_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        from modulo.core.analytics.service import AnalyticsMigrationRequiredError

        patches = _plan_enabled_patches()
        patches.append(
            patch(
                "modulo.api.mcp_server.run_concurrency_query",
                new=AsyncMock(
                    side_effect=AnalyticsMigrationRequiredError("Feature is not available. Run database migrations.")
                ),
            )
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_database_error_maps_to_database_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        from modulo.core.analytics.service import AnalyticsDatabaseError

        patches = _plan_enabled_patches()
        patches.append(
            patch(
                "modulo.api.mcp_server.run_concurrency_query",
                new=AsyncMock(side_effect=AnalyticsDatabaseError("Database temporarily unavailable.")),
            )
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "database_error"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_programming_error_maps_to_migration_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        from sqlalchemy.exc import ProgrammingError

        patches = _plan_enabled_patches()
        patches.append(
            patch(
                "modulo.api.mcp_server.run_concurrency_query",
                new=AsyncMock(side_effect=ProgrammingError("SELECT ...", {}, RuntimeError("no such table"))),
            )
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_unexpected_error_maps_to_internal_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        import contextlib

        patches = _plan_enabled_patches()
        patches.append(
            patch("modulo.api.mcp_server.run_concurrency_query", new=AsyncMock(side_effect=RuntimeError("boom")))
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = await query_analytics_concurrency()
        assert result["error"] == "internal_error"
