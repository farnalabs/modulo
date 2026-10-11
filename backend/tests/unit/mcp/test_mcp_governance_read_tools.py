"""Unit tests for the FAR-696 governance-entity read/list MCP tools.

Covers: list_teams / list_users, list_feature_flags, get_audit_log,
list_notification_deliveries, list_lifecycle_maps / get_lifecycle_map,
list_plugins, list_sso_providers, list_eval_suites / get_eval_suite,
get_schema_versions, get_pipeline_diff.

Authz: every tool maps to the same permission key as the corresponding REST
route and is gated through ``_check_agent_tool_scope``; list_feature_flags
additionally enforces the strict system-admin gate REST's
``require_system_permission`` applies. The pipeline-diff tool must mask
credential-bearing node fields (FAR-1181 parity with the REST route), and the
SSO listing must never surface client secrets.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import (
    get_audit_log,
    get_eval_suite,
    get_lifecycle_map,
    get_pipeline_diff,
    get_schema_versions,
    list_eval_suites,
    list_feature_flags,
    list_lifecycle_maps,
    list_notification_deliveries,
    list_plugins,
    list_sso_providers,
    list_teams,
    list_users,
    mcp,
)
from modulo.db.crud.base import PageResult
from tests.unit.mcp.helpers import ORG_ID, USER_ID, AuthContext, make_session_context

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)

_EXPECTED_FAR696_TOOLS = (
    "list_teams",
    "list_users",
    "list_feature_flags",
    "get_audit_log",
    "list_notification_deliveries",
    "list_lifecycle_maps",
    "get_lifecycle_map",
    "list_plugins",
    "list_sso_providers",
    "list_eval_suites",
    "get_eval_suite",
    "get_schema_versions",
    "get_pipeline_diff",
)


def _page(items: list, next_cursor: str | None = None, total: int | None = None) -> PageResult:
    return PageResult(
        items=items,
        total=total if total is not None else len(items),
        page=1,
        page_size=len(items) or 20,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


class TestFar696ToolRegistration:
    def test_all_tools_registered(self) -> None:
        for name in _EXPECTED_FAR696_TOOLS:
            assert name in mcp._tool_manager._tools, f"tool '{name}' is not registered"
            assert mcp._tool_manager._tools[name].fn is not None

    def test_all_tools_scoped_or_read_only(self) -> None:
        from modulo.core.mcp.scope_validator import READ_ONLY_TOOLS, TOOL_SCOPE_REQUIREMENTS

        for name in _EXPECTED_FAR696_TOOLS:
            assert name in TOOL_SCOPE_REQUIREMENTS or name in READ_ONLY_TOOLS, (
                f"tool '{name}' is neither permission-mapped nor read-only pinned — deny-by-default"
            )


class TestListTeams(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_teams()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.team.list_teams")
    async def test_returns_org_scoped_teams(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        team = MagicMock()
        team.id = uuid.uuid4()
        team.name = "platform"
        team.description = "Platform team"
        team.account_id = USER_ID
        team.created_at = NOW
        mock_list.return_value = _page([team])
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_teams(page=1, page_size=10)

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["id"] == str(team.id)
        assert item["name"] == "platform"
        mock_list.assert_awaited_once()
        assert mock_list.await_args.kwargs["org_id"] == ORG_ID

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_viewer_role_passes_scope(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        # team.list resolves to viewer — the real chokepoint must allow it.
        from modulo.core.mcp.scope_validator import check_tool_scope

        mock_session.return_value = make_session_context(AsyncMock())
        with patch("modulo.db.crud.team.list_teams", new=AsyncMock(return_value=_page([]))):
            result = await list_teams()
        assert "error" not in result
        check_tool_scope("viewer", "list_teams")  # must not raise

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_teams()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_on_programming_error(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_session.return_value.__aenter__.return_value = AsyncMock()
        with patch(
            "modulo.db.crud.team.list_teams",
            new=AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("no table"))),
        ):
            result = await list_teams()
        assert result["error"] == "migration_required"


class TestListUsers(AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        # user.list resolves to admin — happy paths need the admin role.
        _ctx_role.set("admin")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_users()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.routes.admin._list_org_accounts")
    async def test_returns_org_members(
        self,
        mock_accounts: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        account = MagicMock()
        account.id = uuid.uuid4()
        account.email = "alice@example.com"
        account.display_name = "Alice"
        account.active = True
        account.auth_provider = "local"
        account.created_at = NOW
        account.last_login = None
        membership = MagicMock()
        membership.role = "operator"
        membership.deactivated_at = None
        mock_accounts.return_value = ([(account, membership)], 1)
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_users(page=1, page_size=20, search="alice", role="operator")

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["email"] == "alice@example.com"
        assert item["org_role"] == "operator"
        assert item["is_active"] is True
        assert "password_hash" not in item
        assert mock_accounts.await_args.kwargs["org_id"] == ORG_ID

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_role_filter_rejected(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_users(role="superuser")
        assert result["error"] == "validation_failed"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_viewer_denied_by_real_scope_check(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import _ctx_role

        mock_session.return_value = make_session_context(AsyncMock())
        _ctx_role.set("viewer")
        result = await list_users()
        assert result["error"] == "insufficient_scope"


class TestListFeatureFlags(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_feature_flags()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_non_system_admin_denied_even_as_org_admin(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import _ctx_role

        mock_session.return_value = make_session_context(AsyncMock())
        _ctx_role.set("admin")  # org admin — but NOT system admin
        result = await list_feature_flags()
        assert result["error"] == "insufficient_scope"
        assert "system admin" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.license.get_license", return_value=None)
    @patch("modulo.core.feature_flags.FeatureFlagRegistry.from_db", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.resolve_plan_context", new_callable=AsyncMock)
    @patch("modulo.db.crud.organisation.get_organisation", new_callable=AsyncMock)
    async def test_system_admin_sees_flags_with_org_overrides(
        self,
        mock_org: AsyncMock,
        mock_plan: AsyncMock,
        mock_from_db: AsyncMock,
        mock_license: MagicMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import _ctx_is_system_admin, _ctx_role

        org = MagicMock()
        org.settings_json = {"feature_overrides": {"pipeline_diff_rollback": True}}
        mock_org.return_value = org
        plan_ctx = MagicMock()
        plan_ctx.tier.return_value = "community"
        mock_plan.return_value = plan_ctx

        flag = MagicMock()
        flag.name = "pipeline_diff_rollback"
        flag.description = "Snapshot diff + rollback"
        flag.tier = "team"
        flag.currently_active = False
        flag.depends_on = None
        registry = MagicMock()
        registry.current_tier = "community"
        registry.has_license_key = False
        registry.list_flags.return_value = [flag]
        registry.tier_gap_flags.return_value = []
        mock_from_db.return_value = registry

        mock_session.return_value = make_session_context(AsyncMock())
        _ctx_role.set("admin")
        _ctx_is_system_admin.set(True)
        try:
            settings = MagicMock()
            settings.modulo_license_key = ""
            settings.modulo_dev_mode = False
            with patch("modulo.api.mcp_server.get_settings", return_value=settings):
                result = await list_feature_flags()
        finally:
            _ctx_is_system_admin.set(False)

        assert "error" not in result
        (item,) = result["flags"]
        # The org override wins over the registry default (REST parity).
        assert item["currently_active"] is True
        assert result["license"]["tier"] == "community"


class TestGetAuditLog(AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        # audit.manage resolves to admin.
        _ctx_role.set("admin")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await get_audit_log()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.audit_logger.list_audit_events", new_callable=AsyncMock)
    async def test_returns_paged_events(
        self,
        mock_events: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_events.return_value = {
            "items": [{"id": str(uuid.uuid4()), "event_type": "pipeline.created"}],
            "total": 1,
            "next_cursor": None,
            "limit": 50,
        }
        mock_session.return_value = make_session_context(AsyncMock())

        actor = str(uuid.uuid4())
        result = await get_audit_log(
            limit=50,
            event_type="pipeline.created",
            actor_user_id=actor,
            from_date="2026-01-01T00:00:00+00:00",
            to_date="2026-12-31T23:59:59+00:00",
        )

        assert result["total"] == 1
        assert mock_events.await_args.args[1] == ORG_ID
        assert mock_events.await_args.kwargs["event_type"] == "pipeline.created"
        assert mock_events.await_args.kwargs["actor_user_id"] == uuid.UUID(actor)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_actor_uuid_rejected(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_audit_log(actor_user_id="not-a-uuid")
        assert result["error"] == "invalid_id"
        assert result["field"] == "actor_user_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_from_date_rejected(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_audit_log(from_date="yesterday")
        assert result["error"] == "validation_failed"
        assert result["field"] == "from_date"


class TestListNotificationDeliveries(AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        # admin.notification.manage resolves to admin.
        _ctx_role.set("admin")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_notification_deliveries()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_delivery_entries_org_scoped(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        delivery = MagicMock()
        delivery.id = uuid.uuid4()
        delivery.event_type = "run.failed"
        delivery.status = "failed"
        delivery.attempt_count = 3
        delivery.response_code = 500
        delivery.last_error = "upstream 500"
        delivery.response_body = None
        delivery.endpoint_id = uuid.uuid4()
        delivery.created_at = NOW

        rows_result = MagicMock()
        rows_result.all.return_value = [(delivery, "https://hooks.example.com/x")]
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[rows_result, count_result])
        mock_session.return_value = make_session_context(session)

        result = await list_notification_deliveries(status="failed", event_type="run.failed")

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["status"] == "failed"
        assert item["endpoint_url"] == "https://hooks.example.com/x"
        assert result["next_cursor"] is None

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_cursor_rejected(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_notification_deliveries(cursor="not-a-date")
        assert result["error"] == "validation_failed"
        assert result["field"] == "cursor"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_endpoint_id_rejected(self, mock_session: AsyncMock, mock_validate: AsyncMock) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_notification_deliveries(endpoint_id="nope")
        assert result["error"] == "invalid_id"
        assert result["field"] == "endpoint_id"


class TestLifecycleMaps(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_list_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_lifecycle_maps()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.lifecycle_map.service.list_lifecycle_maps", new_callable=AsyncMock)
    async def test_list_returns_maps(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        lm = MagicMock()
        lm.id = uuid.uuid4()
        lm.name = "alpha-onboarding"
        lm.description = "Alpha journey"
        lm.owner_team_id = None
        lm.visibility = "org"
        lm.version = 3
        lm.content_json = {"stages": [], "edges": []}
        lm.archived_at = None
        lm.created_at = NOW
        lm.updated_at = NOW
        mock_list.return_value = _page([lm])
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_lifecycle_maps(page=1, page_size=20)

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["name"] == "alpha-onboarding"
        assert item["content_json"] == {"stages": [], "edges": []}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new_callable=AsyncMock)
    async def test_get_returns_map(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        lm = MagicMock()
        lm.id = uuid.uuid4()
        lm.organisation_id = ORG_ID
        lm.name = "alpha-onboarding"
        lm.description = None
        lm.owner_team_id = None
        lm.visibility = "org"
        lm.version = 1
        lm.content_json = {"stages": []}
        lm.archived_at = None
        lm.created_at = NOW
        lm.updated_at = NOW
        mock_get.return_value = lm
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_lifecycle_map(str(lm.id))

        assert result["id"] == str(lm.id)
        assert result["content_json"] == {"stages": []}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new_callable=AsyncMock)
    async def test_get_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_lifecycle_map(str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.lifecycle_map.service.get_lifecycle_map", new_callable=AsyncMock)
    async def test_get_team_scoped_key_boundary(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import _ctx_team_id

        team_a = uuid.uuid4()
        team_b = uuid.uuid4()
        lm = MagicMock()
        lm.id = uuid.uuid4()
        lm.organisation_id = ORG_ID
        lm.owner_team_id = team_a
        mock_get.return_value = lm
        mock_session.return_value = make_session_context(AsyncMock())
        _ctx_team_id.set(team_b)
        try:
            result = await get_lifecycle_map(str(lm.id))
        finally:
            _ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"


class TestListPlugins(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_plugins()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.core.plugin_registry.get_plugin_registry")
    async def test_returns_registry_metadata(self, mock_registry_factory: MagicMock, mock_validate: AsyncMock) -> None:
        manifest = MagicMock()
        manifest.PLUGIN_ID = "demo-plugin"
        manifest.display_name = "Demo"
        manifest.description = "A demo plugin"
        manifest.version = "1.0.0"
        manifest.capabilities = {"pipeline", "connector"}
        health = MagicMock()
        health.ok = True
        health.detail = "healthy"
        health.checked_at = NOW
        registry = MagicMock()
        registry.list_plugins.return_value = {"demo-plugin": manifest}
        registry.health_check.return_value = {"demo-plugin": health}
        mock_registry_factory.return_value = registry

        result = await list_plugins()

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["id"] == "demo-plugin"
        assert item["health_ok"] is True
        assert item["capabilities"] == ["connector", "pipeline"]


class TestListSsoProviders(AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        # sso.manage resolves to admin.
        _ctx_role.set("admin")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_sso_providers()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.sso_provider.list_providers", new_callable=AsyncMock)
    async def test_never_returns_client_secret(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        provider = MagicMock()
        provider.id = uuid.uuid4()
        provider.provider_type = "oidc"
        provider.name = "Okta"
        provider.provider_id = "okta"
        provider.client_id = "client-123"
        provider.enabled = True
        provider.auto_provision = True
        provider.default_role = "viewer"
        provider.allowed_domains = ["example.com"]
        provider.preset = "okta"
        provider.tenant_domain = None
        provider.discovery_url = "https://example.com/.well-known/openid-configuration"
        provider.metadata_url = None
        provider.entity_id = None
        provider.scopes = ["openid", "email"]
        provider.created_at = NOW
        provider.updated_at = NOW
        mock_list.return_value = [provider]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_sso_providers()

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["name"] == "Okta"
        assert item["client_id"] == "client-123"
        # Config secrets stay human-only — never in the MCP payload.
        assert "client_secret" not in item
        assert "metadata_xml" not in item
        mock_list.assert_awaited_once_with(mock_session.return_value.__aenter__.return_value, org_id=ORG_ID)


class TestEvalSuites(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_list_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_eval_suites()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_list_returns_suites(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        suite = MagicMock()
        suite.id = uuid.uuid4()
        suite.name = "regression"
        suite.description = "Regression suite"
        suite.version = 2
        suite.owner_team_id = None
        suite.visibility = "org"
        suite.eval_definition_ids = [uuid.uuid4()]
        suite.created_at = NOW
        suite.updated_at = NOW

        count_result = MagicMock()
        count_result.scalar.return_value = 1
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [suite]

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        mock_session.return_value = make_session_context(session)

        result = await list_eval_suites(page=1, page_size=20)

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["name"] == "regression"
        assert item["eval_definition_ids"] == [str(suite.eval_definition_ids[0])]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_returns_suite(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        suite = MagicMock()
        suite.id = uuid.uuid4()
        suite.organisation_id = ORG_ID
        suite.name = "regression"
        suite.description = None
        suite.version = 1
        suite.owner_team_id = None
        suite.visibility = "org"
        suite.eval_definition_ids = []
        suite.created_at = NOW
        suite.updated_at = NOW

        row = MagicMock()
        row.scalar_one_or_none.return_value = suite
        session = AsyncMock()
        session.execute = AsyncMock(return_value=row)
        mock_session.return_value = make_session_context(session)

        result = await get_eval_suite(str(suite.id))

        assert result["id"] == str(suite.id)
        assert result["name"] == "regression"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_not_found(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        row = MagicMock()
        row.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute = AsyncMock(return_value=row)
        mock_session.return_value = make_session_context(session)

        result = await get_eval_suite(str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_invalid_uuid_rejected(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_eval_suite("nope")
        assert result["error"] == "invalid_id"
        assert result["field"] == "suite_id"


class TestGetSchemaVersions(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await get_schema_versions(str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.schema.list_schema_versions", new_callable=AsyncMock)
    @patch("modulo.db.crud.schema.get_schema", new_callable=AsyncMock)
    async def test_returns_versions(
        self,
        mock_get_schema: AsyncMock,
        mock_list_versions: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        mock_get_schema.return_value = schema

        sv = MagicMock()
        sv.id = uuid.uuid4()
        sv.schema_id = schema.id
        sv.version = "1.0.0"
        sv.version_number = 1
        sv.definition_json = {"type": "object"}
        sv.published = True
        sv.account_id = USER_ID
        sv.created_at = NOW
        sv.updated_at = NOW
        mock_list_versions.return_value = _page([sv])

        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_schema_versions(str(schema.id), page=1, page_size=20)

        assert result["total"] == 1
        (item,) = result["data"]
        assert item["version"] == "1.0.0"
        assert item["definition_json"] == {"type": "object"}
        assert mock_get_schema.await_args.args[1] == schema.id

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.schema.get_schema", new_callable=AsyncMock)
    async def test_cross_org_schema_reads_not_found(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = uuid.uuid4()  # a DIFFERENT org
        mock_get_schema.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_schema_versions(str(schema.id))
        assert result["error"] == "not_found"


class TestGetPipelineDiff(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await get_pipeline_diff(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.db.crud.pipeline_snapshot_versioning.diff_snapshots", new_callable=AsyncMock)
    async def test_masks_node_credentials_in_diff_payload(
        self,
        mock_diff: AsyncMock,
        mock_owner: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_owner.return_value = None
        secret = "sk-live-ABCDEF1234567890"
        raw_node = {
            "id": "node-1",
            "env_vars": {"OPENAI_API_KEY": secret},
        }
        mock_diff.return_value = {
            "snapshot_a": {"id": "sa", "graph": {"nodes": [dict(raw_node)], "edges": []}},
            "snapshot_b": {"id": "sb", "graph": {"nodes": [], "edges": []}},
            "nodes_added": [],
            "nodes_removed": [dict(raw_node)],
        }
        mock_session.return_value = make_session_context(AsyncMock())

        pid = str(uuid.uuid4())
        result = await get_pipeline_diff(pid, str(uuid.uuid4()), str(uuid.uuid4()))

        assert "error" not in result
        # The raw credential must never survive into the MCP payload —
        # FAR-1181 parity with the REST snapshot-diff route.
        assert secret not in str(result)
        masked_node = result["snapshot_a"]["graph"]["nodes"][0]
        assert masked_node["env_vars"]["OPENAI_API_KEY"] != secret
        # Both snapshot graphs AND nodes_removed are masked (REST parity).
        assert result["nodes_removed"][0]["env_vars"]["OPENAI_API_KEY"] != secret

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.db.crud.pipeline_snapshot_versioning.diff_snapshots", new_callable=AsyncMock)
    async def test_snapshots_not_found(
        self,
        mock_diff: AsyncMock,
        mock_owner: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_owner.return_value = None
        mock_diff.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_pipeline_diff(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._pipeline_owner_team_id", new_callable=AsyncMock)
    @patch("modulo.db.crud.pipeline_snapshot_versioning.diff_snapshots", new_callable=AsyncMock)
    async def test_team_scoped_key_boundary(
        self,
        mock_diff: AsyncMock,
        mock_owner: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import _ctx_team_id

        team_a = uuid.uuid4()
        team_b = uuid.uuid4()
        mock_owner.return_value = team_a
        mock_session.return_value = make_session_context(AsyncMock())
        _ctx_team_id.set(team_b)
        try:
            result = await get_pipeline_diff(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        finally:
            _ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"
        mock_diff.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_snapshot_uuid_rejected(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_pipeline_diff(str(uuid.uuid4()), "not-a-uuid", str(uuid.uuid4()))
        assert result["error"] == "invalid_id"
        assert result["field"] == "snapshot_a_id"
