"""Unit tests for the get_org_config / get_available_features / get_integration_status /
search_documentation / list_housekeeping / perform_housekeeping MCP tools."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import IntegrityError, ProgrammingError

from modulo.api.mcp_server import (
    get_available_features,
    get_integration_status,
    get_org_config,
    list_housekeeping,
    perform_housekeeping,
    search_documentation,
)
from modulo.core.documentation_indexer import DocEntry
from modulo.core.feature_flags import FeatureFlag
from modulo.core.housekeeping import Candidate, CategoryResult
from modulo.db.models.secret import Secret
from tests.unit.mcp.helpers import AuthContext, make_session_context


def _make_config(*, key: str, value: object) -> MagicMock:
    cfg = MagicMock()
    cfg.key = key
    cfg.value = value
    return cfg


def _make_housekeeping_session() -> AsyncMock:
    """AsyncMock session whose begin_nested() returns an async context manager."""
    s = AsyncMock()
    nested = AsyncMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    s.begin_nested = MagicMock(return_value=nested)
    return s


# ---------------------------------------------------------------------------
# get_org_config
# ---------------------------------------------------------------------------


class TestGetOrgConfigErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_org_config()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_section_returns_invalid_section(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config(section="bogus")

        assert result["error"] == "invalid_section"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_migration_required_on_programming_error(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.side_effect = ProgrammingError("SELECT 1", {}, Exception("no table"))
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config()

        assert result["error"] == "migration_required"


class TestGetOrgConfigSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_returns_table_when_config_exists(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.return_value = [
            _make_config(key="default_plan", value="team"),
            _make_config(key="feature_flags:parallel_branches", value={"enabled": True}),
        ]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config()

        assert result["count"] == 2
        assert result["results"].startswith("| Key | Value |")
        assert "| default_plan | team |" in result["results"]
        assert "| feature_flags:parallel_branches |" in result["results"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_sensitive_keys_are_filtered_out(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.return_value = [
            _make_config(key="fernet_key", value="vK-xU7GqHLflg"),
            _make_config(key="app_name", value="modulo"),
        ]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config()

        assert result["count"] == 1
        assert "fernet_key" not in result["results"]
        assert "| app_name | modulo |" in result["results"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_section_filter_limits_config_to_section(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.return_value = [
            _make_config(key="remy_config:enabled", value="true"),
            _make_config(key="default_plan", value="team"),
        ]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config(section="remy")

        assert result["count"] == 1
        assert "remy_config:enabled" in result["results"]
        assert "default_plan" not in result["results"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config", return_value=[])
    async def test_no_config_found_message(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config()

        assert result["count"] == 0
        assert "No configuration found" in result["results"]


# ---------------------------------------------------------------------------
# get_available_features
# ---------------------------------------------------------------------------


class TestGetAvailableFeaturesErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_available_features()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_settings")
    @patch("modulo.core.feature_flags.resolve_plan_context")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_migration_required_on_programming_error(
        self,
        mock_get_org: AsyncMock,
        mock_resolve: AsyncMock,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_settings.return_value = MagicMock()
        mock_resolve.side_effect = ProgrammingError("SELECT 1", {}, Exception("no table"))
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_available_features()

        assert result["error"] == "migration_required"


class TestGetAvailableFeaturesSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_settings")
    @patch("modulo.core.feature_flags.resolve_plan_context")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_returns_tier_and_enabled_features(
        self,
        mock_get_org: AsyncMock,
        mock_resolve: AsyncMock,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_settings.return_value = MagicMock()
        plan_ctx = MagicMock()
        plan_ctx.tier.return_value = "community"
        plan_ctx.list_enabled_features.return_value = [
            FeatureFlag(name="mcp_server", description="MCP", tier="community", currently_active=True),
            FeatureFlag(name="webhook_trigger", description="Webhook", tier="community", currently_active=True),
            FeatureFlag(name="sso", description="SSO", tier="team", currently_active=False),
        ]
        mock_resolve.return_value = plan_ctx
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_available_features()

        assert result["tier"] == "community"
        assert result["feature_count"] == 3
        assert "| Feature | Required Tier | Available |" in result["results"]
        assert "| mcp_server | community | yes |" in result["results"]
        assert "| sso | team | no |" in result["results"]


# ---------------------------------------------------------------------------
# get_integration_status
# ---------------------------------------------------------------------------


class TestGetIntegrationStatusErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_integration_status()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_on_programming_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=ProgrammingError("SELECT 1", {}, Exception("no table")))
        mock_session.return_value = make_session_context(mock_sesh)

        result = await get_integration_status()

        assert result["error"] == "migration_required"


class TestGetIntegrationStatusSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_status_structure(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        last_check = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        connector = MagicMock()
        connector.name = "github-main"
        connector.connector_type_id = "github"
        connector.status = "connected"
        connector.last_health_check_at = last_check
        connector.last_health_check_error = ""

        backend = MagicMock()
        backend.name = "openai"
        backend.provider = "openai"
        backend.model_id = "gpt-4o"
        backend.credentials_ciphertext = b"encrypted-bytes"
        backend.status = "ok"

        connector_result = MagicMock()
        connector_result.scalars.return_value.all.return_value = [connector]
        backend_result = MagicMock()
        backend_result.scalars.return_value.all.return_value = [backend]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 3

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[connector_result, backend_result, count_result])
        mock_session.return_value = make_session_context(mock_sesh)

        result = await get_integration_status()

        assert result["trigger_count"] == 3
        assert result["connectors"] == [
            {
                "name": "github-main",
                "type": "github",
                "status": "connected",
                "last_check": last_check.isoformat(),
                "error": "",
            }
        ]
        assert result["model_backends"] == [
            {
                "name": "openai",
                "provider": "openai",
                "model": "gpt-4o",
                "has_credentials": True,
                "status": "ok",
            }
        ]
        assert "## Connectors (1)" in result["results"]
        assert "## Model Backends (1)" in result["results"]
        assert "Total triggers: 3" in result["results"]


# ---------------------------------------------------------------------------
# search_documentation
# ---------------------------------------------------------------------------


class TestSearchDocumentationErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await search_documentation(query="pipeline")
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._get_doc_index")
    async def test_search_failure_returns_internal_error(
        self,
        mock_get_index: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        index = MagicMock()
        index.search.side_effect = RuntimeError("index build failed")
        mock_get_index.return_value = index

        result = await search_documentation(query="pipeline")

        assert result["error"] == "internal_error"


class TestSearchDocumentationSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._get_doc_index")
    async def test_returns_formatted_results(
        self,
        mock_get_index: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        index = MagicMock()
        index.search.return_value = [
            DocEntry(
                heading_path="3. Runtime > Pipelines",
                heading="Pipelines",
                first_paragraph="Pipelines orchestrate agents.",
            )
        ]
        index.format_results.return_value = "### 3. Runtime > Pipelines\n\nPipelines\n\nPipelines orchestrate agents."
        mock_get_index.return_value = index

        result = await search_documentation(query="pipelines", section="3. Runtime")

        assert result["count"] == 1
        assert "### 3. Runtime > Pipelines" in result["results"]
        index.search.assert_called_once_with("pipelines", section="3. Runtime")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._get_doc_index")
    async def test_no_results_returns_not_found_message(
        self,
        mock_get_index: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        index = MagicMock()
        index.search.return_value = []
        mock_get_index.return_value = index

        result = await search_documentation(query="nonexistent-topic")

        assert result == {"results": "No documentation found for query.", "count": 0}


# ---------------------------------------------------------------------------
# list_housekeeping
# ---------------------------------------------------------------------------


class TestListHousekeepingErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_housekeeping()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_housekeeping()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.housekeeping.scan_all")
    async def test_migration_required_on_programming_error(
        self,
        mock_scan: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_scan.side_effect = ProgrammingError("SELECT 1", {}, Exception("no table"))
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_housekeeping()

        assert result["error"] == "migration_required"


class TestListHousekeepingSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.housekeeping.scan_all")
    async def test_returns_categories_shape(
        self,
        mock_scan: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        candidate = Candidate(
            id="00000000-0000-0000-0000-0000000000aa",
            name="gh_token",
            detail="secret not referenced",
            created_at="2026-01-01T12:00:00+00:00",
            entity_type="secret",
        )
        mock_scan.return_value = [CategoryResult("orphan_secrets", [candidate])]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_housekeeping(limit=100)

        assert result["total_count"] == 1
        assert len(result["categories"]) == 1
        category = result["categories"][0]
        assert category["category"] == "orphan_secrets"
        assert category["label"] == "Orphan Secrets"
        assert category["count"] == 1
        assert category["candidates"] == [candidate.to_dict()]


# ---------------------------------------------------------------------------
# perform_housekeeping
# ---------------------------------------------------------------------------


class TestPerformHousekeepingErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await perform_housekeeping(items=[{"id": "x", "entity_type": "secret"}])
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await perform_housekeeping(items=[{"id": "x", "entity_type": "secret"}])
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_missing_entity_type_or_id_reports_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await perform_housekeeping(items=[{"id": "abc123"}])

        assert result["deleted_count"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error"] == "item missing entity_type or id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.housekeeping.ENTITY_MODEL_MAP", {})
    async def test_unknown_entity_type_reports_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await perform_housekeeping(items=[{"id": "abc123", "entity_type": "bogus_type"}])

        assert result["deleted_count"] == 0
        assert result["errors"] == [{"entity_type": "bogus_type", "error": "Unknown entity type: bogus_type"}]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._delete_housekeeping_group", new_callable=AsyncMock)
    async def test_invalid_org_fk_is_triage_only(
        self,
        mock_delete_group: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await perform_housekeeping(items=[{"id": "abc123", "entity_type": "invalid_org_fk"}])

        assert result["deleted_count"] == 0
        assert result["errors"] == [
            {"entity_type": "invalid_org_fk", "error": "Surfaced for triage only — not auto-deleted."}
        ]
        # Detection-only category must never reach the destructive delete path.
        mock_delete_group.assert_not_awaited()


class TestPerformHousekeepingSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.housekeeping.ENTITY_MODEL_MAP", {"secret": Secret})
    async def test_deletes_valid_entity(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = _make_housekeeping_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()
        mock_sesh.execute = AsyncMock(return_value=mock_result)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await perform_housekeeping(items=[{"id": "abc123", "entity_type": "secret"}])

        assert result == {"deleted_count": 1, "errors": []}
        mock_sesh.execute.assert_awaited_once()
        mock_sesh.delete.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.housekeeping.ENTITY_MODEL_MAP", {"secret": Secret})
    async def test_integrity_error_reports_foreign_key_violation(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = _make_housekeeping_session()
        mock_sesh.execute = AsyncMock(
            side_effect=IntegrityError("DELETE 1", {}, Exception("foreign key constraint violation"))
        )
        mock_session.return_value = make_session_context(mock_sesh)

        result = await perform_housekeeping(items=[{"id": "abc123", "entity_type": "secret"}])

        assert result["deleted_count"] == 0
        assert result["errors"] == [
            {
                "id": "abc123",
                "entity_type": "secret",
                "error": "Foreign key constraint violation",
            }
        ]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.housekeeping.ENTITY_MODEL_MAP", {"secret": Secret})
    async def test_migration_required_on_programming_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = _make_housekeeping_session()
        mock_sesh.execute = AsyncMock(side_effect=ProgrammingError("SELECT 1", {}, Exception("no table")))
        mock_session.return_value = make_session_context(mock_sesh)

        result = await perform_housekeeping(items=[{"id": "abc123", "entity_type": "secret"}])

        assert result["error"] == "migration_required"
