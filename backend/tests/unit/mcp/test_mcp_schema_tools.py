"""Unit tests for the create_schema / create_agent / create_model_backend /
infer_schema / list_schemas / validate_payload MCP tools."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError

from modulo.api.mcp_server import (
    create_agent,
    create_model_backend,
    create_schema,
    infer_schema,
    list_schemas,
    validate_payload,
)
from modulo.core.seed_data.library_schemas import SCHEMAS
from modulo.db.crud.base import PageResult
from tests.unit.mcp.helpers import FERNET_KEY, ORG_ID, USER_ID, AuthContext, make_session_context


def _make_settings(*, dev_mode: bool = True) -> MagicMock:
    settings = MagicMock()
    settings.fernet_key = FERNET_KEY
    settings.modulo_dev_mode = dev_mode
    return settings


# ---------------------------------------------------------------------------
# create_agent
# ---------------------------------------------------------------------------


class TestCreateAgentErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_agent(name="qa", prompt_template="Review PRs")
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
            result = await create_agent(name="qa", prompt_template="Review PRs")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_model_backend_id_returns_internal_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_agent(
            name="qa",
            prompt_template="Review PRs",
            model_backend_id="not-a-uuid",
        )

        assert result["error"] == "internal_error"


class TestCreateAgentSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.create_agent")
    async def test_returns_created_agent_shape(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        created = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.name = "qa-reviewer"
        agent.description = "Reviews PRs"
        agent.is_executable = True
        agent.created_at = created
        mock_create.return_value = agent
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_agent(
            name="qa-reviewer",
            prompt_template="Review PRs",
            description="Reviews PRs",
            is_executable=True,
        )

        assert result["id"] == str(agent.id)
        assert result["name"] == "qa-reviewer"
        assert result["description"] == "Reviews PRs"
        assert result["is_executable"] is True
        assert result["created_at"] == created.isoformat()

        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["org_id"] == ORG_ID
        assert call_kwargs["account_id"] == USER_ID
        assert call_kwargs["prompt_template"] == "Review PRs"


class TestCreateAgentNullableSchemaIds(AuthContext):
    """FAR-31: creating an agent without schema IDs must not send a sentinel UUID."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.create_agent")
    async def test_omitted_ids_pass_none_not_sentinel(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.name = "qa-reviewer"
        agent.description = None
        agent.is_executable = True
        agent.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        mock_create.return_value = agent
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_agent(name="qa-reviewer", prompt_template="Review PRs")

        assert result["id"] == str(agent.id)
        call_kwargs = mock_create.call_args.kwargs
        # The old code sent uuid.UUID(int=0), which failed the schema_versions FK.
        assert call_kwargs["input_schema_id"] is None
        assert call_kwargs["output_schema_id"] is None
        assert call_kwargs["model_backend_id"] is None
        # Version defaults still apply.
        assert call_kwargs["input_schema_version"] == "latest"
        assert call_kwargs["output_schema_version"] == "latest"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.agent.create_agent")
    async def test_provided_ids_are_parsed_to_uuid(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.name = "qa-reviewer"
        agent.description = None
        agent.is_executable = True
        agent.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        mock_create.return_value = agent
        mock_session.return_value = make_session_context(AsyncMock())

        schema_id = uuid.uuid4()
        backend_id = uuid.uuid4()
        await create_agent(
            name="qa-reviewer",
            prompt_template="Review PRs",
            input_schema_id=str(schema_id),
            output_schema_id=str(schema_id),
            model_backend_id=str(backend_id),
        )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["input_schema_id"] == schema_id
        assert call_kwargs["output_schema_id"] == schema_id
        assert call_kwargs["model_backend_id"] == backend_id


# ---------------------------------------------------------------------------
# create_model_backend
# ---------------------------------------------------------------------------


class TestCreateModelBackendErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_model_backend(name="openai", display_name="OpenAI", provider="openai", model_id="gpt-4o")
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
            result = await create_model_backend(
                name="openai", display_name="OpenAI", provider="openai", model_id="gpt-4o"
            )
        assert result["error"] == "insufficient_scope"


class TestCreateModelBackendSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.mcp_setup_handoff.create_handoff")
    @patch("modulo.api.mcp_server.db_create_model_backend")
    async def test_returns_pending_setup_with_handoff(
        self,
        mock_create: AsyncMock,
        mock_handoff: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mb = MagicMock()
        mb.id = uuid.uuid4()
        mb.name = "openai-prod"
        mb.display_name = "OpenAI Prod"
        mb.provider = "openai"
        mb.model_id = "gpt-4o"
        mb.visibility = "org"
        mock_create.return_value = mb
        mock_handoff.return_value = {
            "setup_url": "https://app.modulo.run/setup/model-backend/abc#token=xyz",
            "expires_at": "2026-01-01T12:15:00+00:00",
            "expires_in_minutes": 15,
        }
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_model_backend(
            name="openai-prod",
            display_name="OpenAI Prod",
            provider="openai",
            model_id="gpt-4o",
            default_params={"temperature": 0.2},
        )

        assert result["id"] == str(mb.id)
        assert result["name"] == "openai-prod"
        assert result["display_name"] == "OpenAI Prod"
        assert result["provider"] == "openai"
        assert result["model_id"] == "gpt-4o"
        assert result["status"] == "pending_setup"
        assert result["visibility"] == "org"
        assert result["setup_url"].startswith("https://app.modulo.run/")
        assert result["expires_in_minutes"] == 15

        # The API key is never sent through the tool — only a handoff is created.
        mock_create.assert_awaited_once()
        assert not mock_create.call_args.kwargs["credentials_ciphertext"]
        mock_handoff.assert_awaited_once()


# ---------------------------------------------------------------------------
# infer_schema
# ---------------------------------------------------------------------------


class TestInferSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await infer_schema(input_sample={"name": "x"})
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
            result = await infer_schema(input_sample={"name": "x"})
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.settings.get_settings")
    async def test_dev_mode_required(
        self,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_settings.return_value = _make_settings(dev_mode=False)
        mock_session.return_value = make_session_context(AsyncMock())

        result = await infer_schema(input_sample={"name": "x"})

        assert result["error"] == "internal_error"
        assert "developer mode" in result.get("detail", "")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.settings.get_settings")
    @patch("modulo.db.crud.model_backend.list_model_backends")
    async def test_no_backend_returns_no_backend(
        self,
        mock_list_backends: AsyncMock,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_settings.return_value = _make_settings(dev_mode=True)
        mock_list_backends.return_value = PageResult(items=[], total=0, page=1, page_size=1)
        mock_session.return_value = make_session_context(AsyncMock())

        result = await infer_schema(input_sample={"name": "x"})

        assert result["error"] == "no_backend"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.settings.get_settings")
    @patch("modulo.db.crud.model_backend.list_model_backends")
    @patch("modulo.core.schema_registry.SchemaInferenceService")
    @patch("modulo.core.model_backend_hub.ModelBackendHub")
    @patch("modulo.core.secrets_backend.create_secrets_backend")
    async def test_inference_failure_returns_inference_failed(
        self,
        mock_create_backend: MagicMock,
        mock_hub_cls: MagicMock,
        mock_service_cls: MagicMock,
        mock_list_backends: AsyncMock,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.schema_registry import SchemaInferenceError

        mb = MagicMock()
        mb.id = uuid.uuid4()
        mock_list_backends.return_value = PageResult(items=[mb], total=1, page=1, page_size=1)
        mock_get_settings.return_value = _make_settings(dev_mode=True)

        service = MagicMock()
        service.infer = AsyncMock(side_effect=SchemaInferenceError("model refused to answer"))
        mock_service_cls.return_value = service

        hub = AsyncMock()
        hub.get = AsyncMock(return_value=AsyncMock())
        mock_hub_cls.return_value.__aenter__ = AsyncMock(return_value=hub)
        mock_hub_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_session.return_value = make_session_context(AsyncMock())

        result = await infer_schema(input_sample={"name": "x"})

        assert result["error"] == "inference_failed"


class TestInferSchemaSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.settings.get_settings")
    @patch("modulo.db.crud.model_backend.list_model_backends")
    @patch("modulo.core.schema_registry.SchemaInferenceService")
    @patch("modulo.core.model_backend_hub.ModelBackendHub")
    @patch("modulo.core.secrets_backend.create_secrets_backend")
    async def test_returns_inferred_definition(
        self,
        mock_create_backend: MagicMock,
        mock_hub_cls: MagicMock,
        mock_service_cls: MagicMock,
        mock_list_backends: AsyncMock,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mb = MagicMock()
        mb.id = uuid.uuid4()
        mock_list_backends.return_value = PageResult(items=[mb], total=1, page=1, page_size=1)
        mock_get_settings.return_value = _make_settings(dev_mode=True)

        definition = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        service = MagicMock()
        service.infer = AsyncMock(return_value=definition)
        mock_service_cls.return_value = service

        hub = AsyncMock()
        hub.get = AsyncMock(return_value=AsyncMock())
        mock_hub_cls.return_value.__aenter__ = AsyncMock(return_value=hub)
        mock_hub_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_session.return_value = make_session_context(AsyncMock())

        result = await infer_schema(input_sample={"name": "x"})

        assert result["definition"] == definition
        assert result["sample_count"] == 1


# ---------------------------------------------------------------------------
# create_schema
# ---------------------------------------------------------------------------


def _make_schema_obj(*, name: str, description: str | None = None, abstract_name: str | None = None) -> MagicMock:
    sc = MagicMock()
    sc.id = uuid.uuid4()
    sc.name = name
    sc.description = description
    sc.abstract_name = abstract_name
    sc.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return sc


class TestCreateSchemaErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_schema(name="customer")
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
            result = await create_schema(name="customer")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.db_create_schema")
    async def test_conflict_on_duplicate_name(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_create.side_effect = IntegrityError("INSERT", {}, Exception("duplicate key"))
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_schema(name="customer")

        assert result["error"] == "conflict"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.db_create_schema")
    async def test_migration_required_on_programming_error(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_create.side_effect = ProgrammingError("INSERT", {}, Exception("no table"))
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_schema(name="customer")

        assert result["error"] == "migration_required"


class TestCreateSchemaSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.db_create_schema")
    async def test_returns_created_schema_and_latest_version(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        schema = _make_schema_obj(name="customer", description="customer model", abstract_name="v1")
        mock_create.return_value = schema
        mock_sesh = AsyncMock()
        mock_sesh.add = MagicMock()
        mock_session.return_value = make_session_context(mock_sesh)

        result = await create_schema(name="customer", description="customer model", abstract_name="v1")

        assert result["id"] == str(schema.id)
        assert result["name"] == "customer"
        assert result["description"] == "customer model"
        assert result["abstract_name"] == "v1"
        assert result["created_at"] == schema.created_at.isoformat()

        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["org_id"] == ORG_ID
        assert call_kwargs["account_id"] == USER_ID
        assert call_kwargs["name"] == "customer"
        assert call_kwargs["description"] == "customer model"
        assert call_kwargs["abstract_name"] == "v1"

        # The agent composite FK references schema_versions(schema_id, version),
        # so the tool must also create the "latest" version placeholder.
        mock_sesh.add.assert_called_once()
        added = mock_sesh.add.call_args.args[0]
        assert added.schema_id == schema.id
        assert added.version == "latest"
        assert added.version_number == 0
        assert added.organisation_id == ORG_ID
        assert added.account_id == USER_ID


# ---------------------------------------------------------------------------
# list_schemas
# ---------------------------------------------------------------------------


def _make_schema(*, name: str, abstract_name: str) -> MagicMock:
    sc = MagicMock()
    sc.id = uuid.uuid4()
    sc.name = name
    sc.description = f"schema for {name}"
    sc.abstract_name = abstract_name
    sc.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return sc


class TestListSchemasErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_schemas()
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.db_list_schemas")
    async def test_migration_required_on_programming_error(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list.side_effect = ProgrammingError("SELECT 1", {}, Exception("no table"))
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_schemas()

        assert result["error"] == "migration_required"


class TestListSchemasSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.db_list_schemas")
    async def test_returns_paginated_schema_metadata(
        self,
        mock_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        schema = _make_schema(name="customer", abstract_name="v3")
        mock_list.return_value = PageResult(
            items=[schema],
            total=7,
            page=1,
            page_size=20,
            next_cursor="cursor-abc",
            has_more=True,
        )
        mock_session.return_value = make_session_context(AsyncMock())

        result = await list_schemas(cursor="cursor-abc", limit=20)

        assert result["total"] == 7
        assert result["next_cursor"] == "cursor-abc"
        assert result["has_more"] is True
        assert result["data"] == [
            {
                "id": str(schema.id),
                "name": "customer",
                "description": "schema for customer",
                "version": "v3",
                "created_at": schema.created_at.isoformat(),
            }
        ]


# ---------------------------------------------------------------------------
# validate_payload
# ---------------------------------------------------------------------------


def _make_schema_version(definition: dict) -> MagicMock:
    sv = MagicMock()
    sv.definition_json = definition
    return sv


def _dogfood_definition(name: str) -> dict:
    for entry in SCHEMAS:
        if entry["name"] == name:
            return entry["definition"]
    raise AssertionError(f"Schema '{name}' not defined in library seed data")


class TestValidatePayloadErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await validate_payload(schema_id=str(uuid.uuid4()), payload={})
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await validate_payload(schema_id="not-a-uuid", payload={})

        assert result["error"] == "invalid_id"
        assert result["field"] == "schema_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema", return_value=None)
    async def test_not_found_when_schema_missing(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload={})

        assert result["error"] == "not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema")
    async def test_no_version_when_schema_has_no_versions(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        schema = MagicMock()
        mock_get_schema.return_value = schema

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=result_mock)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload={})

        assert result["error"] == "no_version"


class TestValidatePayloadSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema")
    async def test_valid_payload_returns_valid(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        definition = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        schema = MagicMock()
        mock_get_schema.return_value = schema

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = _make_schema_version(definition)

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=result_mock)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload={"name": "alice"})

        assert result == {"valid": True, "errors": []}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema")
    async def test_invalid_payload_returns_validation_errors(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        definition = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        schema = MagicMock()
        mock_get_schema.return_value = schema

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = _make_schema_version(definition)

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=result_mock)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload={"name": 123})

        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["path"] == "name"
        assert "not of type" in result["errors"][0]["message"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema")
    async def test_invalid_schema_definition_returns_invalid_schema(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        schema = MagicMock()
        mock_get_schema.return_value = schema

        # A definition that fails Draft202012Validator.check_schema (unknown type).
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = _make_schema_version({"type": "totally-unknown-type"})

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=result_mock)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload={})

        assert result["valid"] is False
        assert result["errors"][0]["path"] == "(schema)"


class TestValidatePayloadDogfoodSchemas(AuthContext):
    """Dogfood the schema system (FAR-304): validate_payload against the seeded
    reusable dogfood pipeline input schemas (``ticket-input``, ``pr-review-input``).

    Mirrors the unit-level jsonschema checks in test_dogfood_input_schemas.py but
    exercises the production ``validate_payload`` tool path (latest version lookup).
    """

    @pytest.mark.parametrize(
        ("schema_name", "payload"),
        [
            (
                "ticket-input",
                {
                    "id": "FAR-304",
                    "title": "Dogfood: define a reusable input schema",
                    "body": "Bind a registered schema to the dogfood pipeline input.",
                    "type": "feature",
                    "priority": "high",
                    "labels": ["dogfood"],
                    "repo": "farnalabs/modulo",
                },
            ),
            (
                "pr-review-input",
                {
                    "number": 42,
                    "repository": "farnalabs/modulo",
                    "head-ref": "feat/far-304",
                    "head-sha": "abc123def456",
                    "action": "synchronize",
                },
            ),
        ],
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema")
    async def test_seeded_dogfood_schema_accepts_valid_payload(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
        schema_name: str,
        payload: dict,
    ) -> None:
        schema = MagicMock()
        mock_get_schema.return_value = schema

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = _make_schema_version(_dogfood_definition(schema_name))

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=result_mock)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload=payload)

        assert result == {"valid": True, "errors": []}

    @pytest.mark.parametrize(
        ("schema_name", "payload"),
        [
            ("ticket-input", {"id": "FAR-304", "title": "Missing required type enum"}),
            ("pr-review-input", {"number": "not-an-int", "repository": "farnalabs/modulo"}),
        ],
    )
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_schema")
    async def test_seeded_dogfood_schema_rejects_invalid_payload(
        self,
        mock_get_schema: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
        schema_name: str,
        payload: dict,
    ) -> None:
        schema = MagicMock()
        mock_get_schema.return_value = schema

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = _make_schema_version(_dogfood_definition(schema_name))

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=result_mock)
        mock_session.return_value = make_session_context(mock_sesh)

        result = await validate_payload(schema_id=str(uuid.uuid4()), payload=payload)

        assert result["valid"] is False
        assert result["errors"]
