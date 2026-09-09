"""Unit tests for the FAR-695 build-time entity read/list MCP tools.

Covers: list_agents / get_agent, list_connectors / get_connector,
list_connector_types, list_model_backends / get_model_backend,
list_environment_profiles, list_parameter_schemas.

Authz: every tool maps to the same permission key as the corresponding REST
route and is gated through ``_check_agent_tool_scope``. Credential-leak
guardrails are asserted explicitly: no ciphertext, no secret values — masked
config only.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import (
    get_agent,
    get_connector,
    get_model_backend,
    get_parameter_schema,
    get_parameter_schema_references,
    get_parameter_set,
    list_agents,
    list_connector_types,
    list_connectors,
    list_environment_profiles,
    list_model_backends,
    list_parameter_schemas,
    list_parameter_sets,
    mcp,
    validate_parameter_schema,
)
from modulo.db.crud.base import PageResult
from tests.unit.mcp.helpers import ORG_ID, AuthContext, make_session_context

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _page(items: list, next_cursor: str | None = None, total: int | None = None) -> PageResult:
    return PageResult(
        items=items,
        total=total if total is not None else len(items),
        page=1,
        page_size=len(items) or 20,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


# ---------------------------------------------------------------------------
# list_agents / get_agent
# ---------------------------------------------------------------------------


class TestListAgents(AuthContext):
    def test_registered_tool_binds_to_list_agents(self) -> None:
        registered = mcp._tool_manager._tools["list_agents"]
        assert registered.fn is list_agents
        import inspect

        params = inspect.signature(registered.fn).parameters
        assert "cursor" in params
        assert "limit" in params

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_agents()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_agents()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.list_agents")
    async def test_returns_org_scoped_summaries_without_prompts(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.name = "triage-agent"
        agent.description = "Triage"
        agent.is_executable = True
        agent.model_backend_id = uuid.uuid4()
        agent.input_schema_id = None
        agent.output_schema_id = None
        agent.created_at = NOW
        agent.prompt_template = "secret prompt body"
        mock_list.return_value = _page([agent])
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_agents(cursor="aGVsbG86MDAwMDAwMDA=", limit=5)

        assert result["total"] == 1
        assert result["has_more"] is False
        (item,) = result["data"]
        assert item["id"] == str(agent.id)
        assert item["name"] == "triage-agent"
        assert item["model_backend_id"] == str(agent.model_backend_id)
        assert "prompt_template" not in item
        # Cursor is threaded through to the CRUD layer.
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["cursor"] == "aGVsbG86MDAwMDAwMDA="
        assert call_kwargs["page_size"] == 5

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.list_agents")
    async def test_migration_required_on_programming_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_list.side_effect = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_agents()
        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.list_agents")
    async def test_generic_error_returns_tool_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_list.side_effect = RuntimeError("boom")
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_agents()
        assert result["error"] == "internal_error"


class TestGetAgent(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await get_agent(agent_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.get_agent")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_agent(agent_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.get_agent")
    async def test_cross_org_row_reads_as_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from tests.unit.mcp.helpers import USER_ID

        other_org = uuid.uuid4()
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.organisation_id = other_org
        agent.account_id = USER_ID
        mock_get.return_value = agent
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_agent(agent_id=str(agent.id))

        assert result["error"] == "not_found"

        mock_validate.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.get_agent")
    async def test_returns_full_definition(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.organisation_id = ORG_ID
        agent.name = "review-agent"
        agent.description = None
        agent.is_executable = True
        agent.prompt_template = "You are a reviewer."
        agent.prompt_version_history = [{"version": "v1"}]
        agent.model_backend_id = uuid.uuid4()
        agent.input_schema_id = uuid.uuid4()
        agent.input_schema_version = "latest"
        agent.output_schema_id = None
        agent.output_schema_version = None
        agent.parameter_schema_id = uuid.uuid4()
        agent.connector_type_refs = [{"connector_type": "github"}]
        agent.required_environment_capabilities = ["git"]
        agent.retry_policy = {"max_attempts": 2}
        agent.token_budget = 1000
        agent.max_input_length = None
        agent.agent_command = None
        agent.created_at = NOW
        agent.updated_at = NOW
        mock_get.return_value = agent
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_agent(agent_id=str(agent.id))

        assert result["id"] == str(agent.id)
        assert result["prompt_template"] == "You are a reviewer."
        assert result["parameter_schema_id"] == str(agent.parameter_schema_id)
        assert result["connector_type_refs"] == [{"connector_type": "github"}]


# ---------------------------------------------------------------------------
# list_connectors / get_connector / list_connector_types
# ---------------------------------------------------------------------------


class TestListConnectors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_connectors()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_connectors()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.list_connector_instances")
    async def test_returns_masked_metadata_without_ciphertext(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.name = "github-primary"
        ci.connector_type_id = "github"
        ci.credentials_ciphertext = b"encrypted-blob"
        ci.config_json = {"api_key": "sk-super-secret", "repo": "farnalabs/modulo"}
        ci.allowed_operations = ["list_files"]
        ci.status = "active"
        ci.visibility = "org"
        ci.owner_team_id = None
        ci.tier = "native"
        ci.created_at = NOW
        ci.updated_at = NOW
        mock_list.return_value = _page([ci], next_cursor="next-page-token", total=7)
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_connectors(cursor=None, limit=20)

        assert result["total"] == 7
        assert result["next_cursor"] == "next-page-token"
        assert result["has_more"] is True
        (item,) = result["data"]
        assert item["id"] == str(ci.id)
        assert item["connector_type_id"] == "github"
        assert item["has_credentials"] is True
        # Credential material must never appear in the response.
        assert "credentials_ciphertext" not in item
        assert "encrypted-blob" not in str(result)
        assert "sk-super-secret" not in str(result)
        # Sensitive config keys are masked; non-sensitive values pass through.
        assert item["config_json"]["api_key"] != "sk-super-secret"
        assert item["config_json"]["repo"] == "farnalabs/modulo"
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["organisation_id"] == ORG_ID
        assert call_kwargs["page_size"] == 20

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.list_connector_instances")
    async def test_migration_required_on_programming_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_list.side_effect = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_connectors()
        assert result["error"] == "migration_required"


class TestGetConnector(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await get_connector(connector_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.get_connector_instance")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_connector(connector_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.get_connector_instance")
    async def test_cross_org_row_reads_as_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.organisation_id = uuid.uuid4()  # not ORG_ID
        mock_get.return_value = ci
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_connector(connector_id=str(ci.id))

        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.get_connector_instance")
    async def test_returns_detail_with_masked_config(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.organisation_id = ORG_ID
        ci.name = "slack-ops"
        ci.connector_type_id = "slack"
        ci.credentials_ciphertext = b"ciphertext"
        ci.config_json = {"token": "xoxb-secret"}
        ci.allowed_operations = []
        ci.status = "active"
        ci.visibility = "team"
        ci.owner_team_id = uuid.uuid4()
        ci.tier = "native"
        ci.created_at = NOW
        ci.updated_at = NOW
        ci.degraded_at = NOW
        ci.last_skip_error = "rate limited"
        mock_get.return_value = ci
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_connector(connector_id=str(ci.id))

        assert result["name"] == "slack-ops"
        assert result["owner_team_id"] == str(ci.owner_team_id)
        assert result["degraded_at"] == NOW.isoformat()
        assert result["last_skip_error"] == "rate limited"
        assert "credentials_ciphertext" not in result
        assert "xoxb-secret" not in str(result)


class TestListConnectorTypes(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_connector_types()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_insufficient_scope(self, mock_validate: AsyncMock) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_connector_types()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_returns_catalogue_with_field_metadata(self, mock_validate: AsyncMock) -> None:
        result = await list_connector_types()

        assert "error" not in result
        items = result["data"]
        ids = [item["id"] for item in items]
        assert result["total"] == len(items)
        assert len(ids) == len(set(ids))
        assert "github" in ids
        (github,) = (item for item in items if item["id"] == "github")
        assert isinstance(github["capabilities"], list) and github["capabilities"]
        # Definition-backed types carry credential FIELD metadata (never values).
        (sentry,) = (item for item in items if item["id"] == "sentry")
        assert sentry["credential_fields"] == {"token": {"required": True}}
        assert "organization_slug" in sentry["config_fields"]
        # No metadata key leaks an actual credential value.
        assert "sk-" not in str(items)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_generic_error_returns_tool_error(self, mock_validate: AsyncMock) -> None:
        with patch.dict("sys.modules", {"modulo.connectors.base": None}):
            result = await list_connector_types()
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# list_model_backends / get_model_backend
# ---------------------------------------------------------------------------


class TestListModelBackends(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_model_backends()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_model_backends()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.model_backend.list_model_backends")
    async def test_returns_metadata_with_credential_presence_only(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mb = MagicMock()
        mb.id = uuid.uuid4()
        mb.name = "prod-openai"
        mb.display_name = "Prod OpenAI"
        mb.provider = "openai"
        mb.model_id = "gpt-5"
        mb.credentials_ciphertext = b"encrypted"
        mb.default_params = {"temperature": 0.2}
        mb.visibility = "org"
        mb.owner_team_id = None
        mb.tier = "native"
        mb.status = "active"
        mb.created_at = NOW
        mb.updated_at = NOW
        mock_list.return_value = _page([mb], next_cursor="cursor-2")
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_model_backends(cursor="cursor-1", limit=10)

        assert result["next_cursor"] == "cursor-2"
        (item,) = result["data"]
        assert item["provider"] == "openai"
        assert item["has_credentials"] is True
        assert "credentials_ciphertext" not in item
        assert "encrypted" not in str(result)
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["org_id"] == ORG_ID
        assert call_kwargs["cursor"] == "cursor-1"
        assert call_kwargs["page_size"] == 10

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.model_backend.list_model_backends")
    async def test_generic_error_returns_tool_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_list.side_effect = RuntimeError("boom")
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_model_backends()
        assert result["error"] == "internal_error"


class TestGetModelBackend(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await get_model_backend(model_backend_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.model_backend.get_model_backend")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_model_backend(model_backend_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.model_backend.get_model_backend")
    async def test_cross_org_row_reads_as_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mb = MagicMock()
        mb.id = uuid.uuid4()
        mb.organisation_id = uuid.uuid4()  # not ORG_ID
        mock_get.return_value = mb
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_model_backend(model_backend_id=str(mb.id))

        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.model_backend.get_model_backend")
    async def test_returns_detail_without_credentials(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        fallback_id = uuid.uuid4()
        mb = MagicMock()
        mb.id = uuid.uuid4()
        mb.organisation_id = ORG_ID
        mb.name = "fallback-claude"
        mb.display_name = "Fallback Claude"
        mb.provider = "anthropic"
        mb.model_id = "claude-sonnet"
        mb.credentials_ciphertext = b"ciphertext"
        mb.default_params = {}
        mb.visibility = "org"
        mb.owner_team_id = None
        mb.tier = "preview"
        mb.status = "active"
        mb.created_at = NOW
        mb.updated_at = NOW
        mb.fallback_backend_ids = [fallback_id]
        mb.cost_tracking = "enabled"
        mb.currency = "USD"
        mock_get.return_value = mb
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_model_backend(model_backend_id=str(mb.id))

        assert result["id"] == str(mb.id)
        assert result["fallback_backend_ids"] == [str(fallback_id)]
        assert result["cost_tracking"] == "enabled"
        assert "credentials_ciphertext" not in result


# ---------------------------------------------------------------------------
# list_environment_profiles
# ---------------------------------------------------------------------------


class TestListEnvironmentProfiles(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_environment_profiles()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_environment_profiles()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.environment_profile.list_environment_profiles")
    async def test_returns_profiles_with_masked_config(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        profile = MagicMock()
        profile.id = uuid.uuid4()
        profile.name = "e2b-heavy"
        profile.description = "E2B runner"
        profile.provider_type = "e2b"
        profile.image_ref = "docker.io/modulo/runner:latest"
        profile.capabilities_json = ["git", "docker"]
        profile.config_json = {"api_key": "super-secret-value", "cpu": 4}
        profile.network_policy = "outbound"
        profile.initialisation_strategy = "git_clone"
        profile.secret_refs_json = ["GITHUB_TOKEN"]
        profile.persistence_policy = "ephemeral"
        profile.status = "active"
        profile.visibility = "org"
        profile.owner_team_id = None
        profile.created_at = NOW
        mock_list.return_value = _page([profile])
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_environment_profiles(limit=200)

        (item,) = result["data"]
        assert item["provider_type"] == "e2b"
        assert item["capabilities"] == ["git", "docker"]
        assert item["config_json"]["cpu"] == 4
        # Sensitive config values are masked, secret VALUES never appear.
        assert item["config_json"]["api_key"] != "super-secret-value"
        assert "super-secret-value" not in str(result)
        # secret_refs are vault key REFERENCES (same surface as the REST route),
        # never the secret values themselves.
        assert item["secret_refs"] == ["GITHUB_TOKEN"]
        # Limit is clamped to the 100 max shared by the other list tools.
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["page_size"] == 100

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.environment_profile.list_environment_profiles")
    async def test_migration_required_on_programming_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_list.side_effect = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_environment_profiles()
        assert result["error"] == "migration_required"


# ---------------------------------------------------------------------------
# list_parameter_schemas
# ---------------------------------------------------------------------------


class TestListParameterSchemas(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate: AsyncMock) -> None:
        result = await list_parameter_schemas()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await list_parameter_schemas()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.list_schemas")
    async def test_returns_org_scoped_schemas(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.name = "deploy-params"
        schema.description = "Deployment parameters"
        schema.version = 3
        schema.parameters = [{"name": "region", "type": "string", "required": True}]
        schema.created_at = NOW
        mock_list.return_value = _page([schema], next_cursor="ps-cursor")
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_parameter_schemas(cursor="ps-prev", limit=50)

        assert result["next_cursor"] == "ps-cursor"
        (item,) = result["data"]
        assert item["id"] == str(schema.id)
        assert item["version"] == 3
        assert item["parameters"] == [{"name": "region", "type": "string", "required": True}]
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["org_id"] == ORG_ID
        assert call_kwargs["cursor"] == "ps-prev"
        assert call_kwargs["limit"] == 50

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.list_schemas")
    async def test_generic_error_returns_tool_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_list.side_effect = RuntimeError("boom")
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_parameter_schemas()
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# get_parameter_schema
# ---------------------------------------------------------------------------


class TestGetParameterSchema(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await get_parameter_schema(schema_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_schema(schema_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_cross_org_row_reads_as_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = uuid.uuid4()
        mock_get.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_schema(schema_id=str(schema.id))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_returns_full_definition(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        schema.name = "deploy-params"
        schema.description = "Deployment parameters"
        schema.version = 3
        schema.parameters = [{"name": "region", "type": "string", "required": True}]
        schema.created_at = NOW
        schema.updated_at = NOW
        mock_get.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_schema(schema_id=str(schema.id))
        assert result["data"]["id"] == str(schema.id)
        assert result["data"]["version"] == 3
        assert result["data"]["parameters"] == [{"name": "region", "type": "string", "required": True}]


# ---------------------------------------------------------------------------
# get_parameter_schema_references
# ---------------------------------------------------------------------------


class TestGetParameterSchemaReferences(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await get_parameter_schema_references(schema_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_schema_references(schema_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_schema.get_schema_references")
    async def test_returns_references(
        self,
        mock_refs: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        mock_get.return_value = schema
        mock_refs.return_value = {"agents": [uuid.uuid4()], "sets": [uuid.uuid4()]}
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_schema_references(schema_id=str(schema.id))
        assert "data" in result
        assert len(result["data"]["agents"]) == 1
        assert len(result["data"]["sets"]) == 1


# ---------------------------------------------------------------------------
# validate_parameter_schema
# ---------------------------------------------------------------------------


class TestValidateParameterSchema(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await validate_parameter_schema(schema_id="not-a-uuid", values={})
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await validate_parameter_schema(schema_id=str(uuid.uuid4()), values={})
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_valid_values_return_valid(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        schema.parameters = [{"name": "region", "type": "string", "required": True}]
        mock_get.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await validate_parameter_schema(schema_id=str(schema.id), values={"region": "us-east-1"})
        assert result["data"]["valid"] is True
        assert not result["data"]["errors"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_required_field_missing_returns_error(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        schema.parameters = [{"name": "region", "type": "string", "required": True}]
        mock_get.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await validate_parameter_schema(schema_id=str(schema.id), values={})
        assert result["data"]["valid"] is False
        assert len(result["data"]["errors"]) == 1
        assert result["data"]["errors"][0]["field"] == "region"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_type_mismatch_returns_error(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        schema.parameters = [{"name": "count", "type": "number"}]
        mock_get.return_value = schema
        mock_session.return_value = make_session_context(AsyncMock())
        result = await validate_parameter_schema(schema_id=str(schema.id), values={"count": "not-a-number"})
        assert result["data"]["valid"] is False
        assert "Expected a numeric value" in result["data"]["errors"][0]["message"]


# ---------------------------------------------------------------------------
# list_parameter_sets
# ---------------------------------------------------------------------------


class TestListParameterSets(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await list_parameter_sets(schema_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    async def test_schema_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_parameter_sets(schema_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_schema.get_schema")
    @patch("modulo.db.crud.parameter_set.list_sets")
    async def test_returns_sets(
        self,
        mock_list: AsyncMock,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = ORG_ID
        mock_get.return_value = schema
        ps = MagicMock()
        ps.id = uuid.uuid4()
        ps.parameter_schema_id = schema.id
        ps.name = "production"
        ps.description = "Production values"
        ps.version = 1
        ps.schema_version = 3
        ps.values = {"region": "us-east-1"}
        ps.created_at = NOW
        ps.updated_at = NOW
        mock_list.return_value = [ps]
        mock_session.return_value = make_session_context(AsyncMock())
        result = await list_parameter_sets(schema_id=str(schema.id))
        assert len(result["data"]) == 1
        assert result["data"][0]["name"] == "production"


# ---------------------------------------------------------------------------
# get_parameter_set
# ---------------------------------------------------------------------------


class TestGetParameterSet(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_uuid_returns_invalid_id(self, mock_validate: AsyncMock) -> None:
        result = await get_parameter_set(schema_id="not-a-uuid", set_id="also-not-a-uuid")
        assert result["error"] == "invalid_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.get_set")
    async def test_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        mock_get.return_value = None
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_set(schema_id=str(uuid.uuid4()), set_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.get_set")
    async def test_wrong_schema_returns_not_found(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        ps = MagicMock()
        ps.id = uuid.uuid4()
        ps.parameter_schema_id = uuid.uuid4()  # different from requested schema_id
        ps.organisation_id = ORG_ID
        mock_get.return_value = ps
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_set(schema_id=str(uuid.uuid4()), set_id=str(ps.id))
        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.parameter_set.get_set")
    async def test_returns_set(
        self,
        mock_get: AsyncMock,
        mock_session: AsyncMock,
        mock_validate: AsyncMock,
    ) -> None:
        schema_id = uuid.uuid4()
        ps = MagicMock()
        ps.id = uuid.uuid4()
        ps.parameter_schema_id = schema_id
        ps.organisation_id = ORG_ID
        ps.name = "staging"
        ps.description = "Staging values"
        ps.version = 2
        ps.schema_version = 3
        ps.values = {"region": "eu-west-1"}
        ps.created_at = NOW
        ps.updated_at = NOW
        mock_get.return_value = ps
        mock_session.return_value = make_session_context(AsyncMock())
        result = await get_parameter_set(schema_id=str(schema_id), set_id=str(ps.id))
        assert result["data"]["name"] == "staging"
        assert result["data"]["version"] == 2
