"""Unit tests for Remy MCP context tools.

Covers the MCP-tool wrappers (``search_documentation``, ``get_integration_status``,
``get_org_config``, ``get_available_features``) and the ``_is_sensitive_key`` guard
that redacts secrets from org-config output.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    SENSITIVE_CONFIG_KEYS,
    _ctx_org_id,
    _get_doc_index,
    _is_sensitive_key,
    get_available_features,
    get_integration_status,
    get_org_config,
    search_documentation,
)
from modulo.core.documentation_indexer import DocEntry, DocumentationIndex

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _mock_connector(**overrides: object) -> MagicMock:
    c = MagicMock()
    c.name = overrides.get("name", "Slack")
    c.connector_type_id = overrides.get("connector_type_id", "slack_webhook")
    c.status = overrides.get("status", "healthy")
    c.last_health_check_at = overrides.get("last_health_check_at")
    c.last_health_check_error = overrides.get("last_health_check_error", "")
    return c


def _mock_backend(**overrides: object) -> MagicMock:
    b = MagicMock()
    b.name = overrides.get("name", "Claude")
    b.provider = overrides.get("provider", "anthropic")
    b.model_id = overrides.get("model_id", "claude-sonnet-4")
    b.credentials_ciphertext = overrides.get("credentials_ciphertext", b"cipher")
    b.status = overrides.get("status", "active")
    return b


class TestSensitiveKeyDetection:
    """Tests for _is_sensitive_key."""

    def test_exact_match_sensitive(self) -> None:
        for key in SENSITIVE_CONFIG_KEYS:
            assert _is_sensitive_key(key), f"{key} should be sensitive"

    def test_prefix_match_sensitive(self) -> None:
        assert _is_sensitive_key("secret_key_backup")
        assert _is_sensitive_key("database_url_primary")
        assert _is_sensitive_key("modulo_license_key_v2")

    def test_non_sensitive_keys(self) -> None:
        assert not _is_sensitive_key("system_prompt")
        assert not _is_sensitive_key("remy_config:org-1")
        assert not _is_sensitive_key("feature_flags")
        assert not _is_sensitive_key("rate_limits")

    def test_case_insensitive_matching(self) -> None:
        assert _is_sensitive_key("SECRET_KEY")
        assert _is_sensitive_key("Database_URL")
        assert _is_sensitive_key("Modulo_License_Key")


class TestSearchDocumentationTool:
    """Tests for the search_documentation MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_org(self) -> None:
        token = _ctx_org_id.set(ORG_ID)
        yield
        _ctx_org_id.reset(token)

    async def test_returns_no_results_message_when_no_match(self) -> None:
        index = DocumentationIndex(entries=[])
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._get_doc_index", return_value=index),
        ):
            result = await search_documentation("nonexistent-topic")
        assert result == {"results": "No documentation found for query.", "count": 0}

    async def test_returns_formatted_results(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(
                    heading_path="Pipelines > Overview",
                    heading="Pipeline Overview",
                    first_paragraph="Pipelines are the core execution unit.",
                ),
                DocEntry(
                    heading_path="Pipelines > Config",
                    heading="Pipeline Config",
                    first_paragraph="Configure pipeline nodes.",
                ),
            ]
        )
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._get_doc_index", return_value=index),
        ):
            result = await search_documentation("pipeline")
        assert result["count"] == 2
        assert "Pipeline Overview" in result["results"]
        assert "---" in result["results"]

    async def test_section_filter_is_forwarded(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(heading_path="Pipelines > Overview", heading="Pipeline Overview", first_paragraph="Core."),
                DocEntry(heading_path="Schemas > Types", heading="Schema Types", first_paragraph="Types."),
            ]
        )
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._get_doc_index", return_value=index),
        ):
            result = await search_documentation("overview", section="Pipelines")
        assert result["count"] == 1
        assert "Pipeline Overview" in result["results"]
        assert "Schema Types" not in result["results"]

    async def test_returns_auth_error_when_unauthenticated(self) -> None:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=False):
            result = await search_documentation("pipeline")
        assert result["error"] == "auth_expired"

    async def test_returns_internal_error_on_failure(self) -> None:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._get_doc_index", side_effect=RuntimeError("boom")),
        ):
            result = await search_documentation("pipeline")
        assert result == {"error": "internal_error", "detail": "Failed to search documentation"}


class TestGetIntegrationStatus:
    """Tests for the get_integration_status MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_org(self) -> None:
        token = _ctx_org_id.set(ORG_ID)
        yield
        _ctx_org_id.reset(token)

    async def test_empty_org_returns_placeholders(self) -> None:
        connector_result = MagicMock()
        connector_result.scalars.return_value.all.return_value = []
        backend_result = MagicMock()
        backend_result.scalars.return_value.all.return_value = []
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[connector_result, backend_result, count_result])

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value.__aenter__.return_value = session
            result = await get_integration_status()

        assert "No connectors configured." in result["results"]
        assert "No model backends configured." in result["results"]
        assert "Total triggers: 0" in result["results"]
        assert not result["connectors"]
        assert not result["model_backends"]
        assert result["trigger_count"] == 0

    async def test_returns_connector_and_backend_rows(self) -> None:
        connector = _mock_connector(
            name="Slack",
            connector_type_id="slack_webhook",
            status="healthy",
            last_health_check_at=None,
            last_health_check_error="",
        )
        backend = _mock_backend(
            name="Claude",
            provider="anthropic",
            model_id="claude-sonnet-4",
            credentials_ciphertext=b"cipher",
            status="active",
        )
        connector_result = MagicMock()
        connector_result.scalars.return_value.all.return_value = [connector]
        backend_result = MagicMock()
        backend_result.scalars.return_value.all.return_value = [backend]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[connector_result, backend_result, count_result])

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value.__aenter__.return_value = session
            result = await get_integration_status()

        assert "## Connectors (1)" in result["results"]
        assert "| Slack | slack_webhook | healthy | never |" in result["results"]
        assert "## Model Backends (1)" in result["results"]
        assert "| Claude | anthropic | claude-sonnet-4 | yes | active |" in result["results"]
        assert "Total triggers: 2" in result["results"]
        assert result["connectors"][0]["name"] == "Slack"
        assert result["connectors"][0]["last_check"] == "never"
        assert result["model_backends"][0]["has_credentials"] is True

    async def test_missing_credentials_reported_as_no(self) -> None:
        backend = _mock_backend(credentials_ciphertext=None)
        connector_result = MagicMock()
        connector_result.scalars.return_value.all.return_value = []
        backend_result = MagicMock()
        backend_result.scalars.return_value.all.return_value = [backend]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[connector_result, backend_result, count_result])

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value.__aenter__.return_value = session
            result = await get_integration_status()

        assert result["model_backends"][0]["has_credentials"] is False
        assert "| Claude | anthropic | claude-sonnet-4 | no | active |" in result["results"]

    async def test_returns_auth_error_when_unauthenticated(self) -> None:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=False):
            result = await get_integration_status()
        assert result["error"] == "auth_expired"


class TestGetOrgConfig:
    """Tests for the get_org_config MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_org(self) -> None:
        token = _ctx_org_id.set(ORG_ID)
        yield
        _ctx_org_id.reset(token)

    def _config(self, key: str, value: object) -> MagicMock:
        cfg = MagicMock()
        cfg.key = key
        cfg.value = value
        return cfg

    async def test_remy_section_filters_and_keeps_safe_keys(self) -> None:
        configs = [
            self._config(f"remy_config:{ORG_ID}", {"system_prompt": "Helpful."}),
            self._config("feature_flags", {"remy_enabled": True}),
        ]
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch(
                "modulo.db.crud.system_config.list_config",
                new_callable=AsyncMock,
                return_value=configs,
            ) as mock_list,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await get_org_config(section="remy")

        mock_list.assert_awaited_once()
        assert result["count"] == 1
        assert f"remy_config:{ORG_ID}" in result["results"]
        assert "feature_flags" not in result["results"]

    async def test_sensitive_keys_redacted(self) -> None:
        configs = [
            self._config("remy_config:org-1", {"a": 1}),
            self._config("secret_key", "sensitive"),
            self._config("database_url", "postgres://..."),
        ]
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.system_config.list_config", new_callable=AsyncMock, return_value=configs),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await get_org_config()

        assert result["count"] == 1
        assert "secret_key" not in result["results"]
        assert "database_url" not in result["results"]
        assert "remy_config:org-1" in result["results"]

    async def test_long_values_truncated(self) -> None:
        configs = [self._config("rate_limits", {"policy": "x" * 500})]
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.system_config.list_config", new_callable=AsyncMock, return_value=configs),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await get_org_config()

        assert result["count"] == 1
        assert "..." in result["results"]
        assert "x" * 500 not in result["results"]

    async def test_dict_values_rendered_as_json(self) -> None:
        configs = [self._config("rate_limits", {"nested": {"key": "value"}})]
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.system_config.list_config", new_callable=AsyncMock, return_value=configs),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await get_org_config()

        assert '"nested"' in result["results"]
        assert '"key"' in result["results"]

    async def test_no_config_found_returns_count_zero(self) -> None:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.system_config.list_config", new_callable=AsyncMock, return_value=[]),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await get_org_config(section="plan")

        assert result["count"] == 0
        assert "No configuration found" in result["results"]

    async def test_invalid_section_returns_error(self) -> None:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            result = await get_org_config(section="bogus")
        assert result["error"] == "invalid_section"
        assert "remy" in result["detail"]

    async def test_returns_auth_error_when_unauthenticated(self) -> None:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=False):
            result = await get_org_config(section="remy")
        assert result["error"] == "auth_expired"


class TestGetAvailableFeatures:
    """Tests for the get_available_features MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_org(self) -> None:
        token = _ctx_org_id.set(ORG_ID)
        yield
        _ctx_org_id.reset(token)

    def _flag(self, name: str, tier: str, active: bool) -> SimpleNamespace:
        return SimpleNamespace(name=name, tier=tier, currently_active=active)

    def _plan_ctx(self, flags: list[SimpleNamespace], tier: str) -> SimpleNamespace:
        return SimpleNamespace(
            tier=lambda: tier,
            list_enabled_features=lambda: flags,
        )

    async def test_returns_feature_table_and_count(self) -> None:
        flags = [
            self._flag("remy_chat", "core", True),
            self._flag("custom_skills", "enterprise", False),
        ]
        plan_ctx = self._plan_ctx(flags, "community")

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.organisation.get_organisation", new_callable=AsyncMock),
            patch("modulo.core.feature_flags.resolve_plan_context", new_callable=AsyncMock, return_value=plan_ctx),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await get_available_features()

        assert result["tier"] == "community"
        assert result["feature_count"] == 2
        assert "remy_chat" in result["results"]
        assert "custom_skills" in result["results"]
        assert "| remy_chat | core | yes |" in result["results"]
        assert "| custom_skills | enterprise | no |" in result["results"]

    async def test_returns_auth_error_when_unauthenticated(self) -> None:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=False):
            result = await get_available_features()
        assert result["error"] == "auth_expired"


class TestDocIndexCache:
    """Tests for the module-level doc index cache."""

    def test_get_doc_index_returns_instance(self) -> None:
        idx = _get_doc_index()
        assert isinstance(idx, DocumentationIndex)
