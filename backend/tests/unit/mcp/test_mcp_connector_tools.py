"""Unit tests for the create_connector / delete_connector MCP tools."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import create_connector, delete_connector

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_FERNET_KEY = "vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI="


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class _AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_user_id.set(None)


# ---------------------------------------------------------------------------
# create_connector
# ---------------------------------------------------------------------------


class TestCreateConnectorErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_connector(name="gh", connector_type_id="github", credentials="sk-secret")
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = _make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await create_connector(name="gh", connector_type_id="github", credentials="sk-secret")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.create_connector_instance")
    async def test_migration_required_on_programming_error(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_create.side_effect = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
        mock_session.return_value = _make_session_context(AsyncMock())
        settings = MagicMock()
        settings.fernet_key = _FERNET_KEY

        with patch("modulo.api.mcp_server.get_settings", return_value=settings):
            result = await create_connector(name="gh", connector_type_id="github", credentials="sk-secret")

        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.create_connector_instance")
    async def test_generic_error_returns_internal_error(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_create.side_effect = RuntimeError("boom")
        mock_session.return_value = _make_session_context(AsyncMock())
        settings = MagicMock()
        settings.fernet_key = _FERNET_KEY

        with patch("modulo.api.mcp_server.get_settings", return_value=settings):
            result = await create_connector(name="gh", connector_type_id="github", credentials="sk-secret")

        assert result["error"] == "internal_error"


class TestCreateConnectorSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.create_connector_instance")
    async def test_returns_created_connector_with_encrypted_credentials(
        self,
        mock_create: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.name = "github-primary"
        ci.connector_type_id = "github"
        ci.visibility = "org"
        mock_create.return_value = ci
        mock_session.return_value = _make_session_context(AsyncMock())
        settings = MagicMock()
        settings.fernet_key = _FERNET_KEY

        with patch("modulo.api.mcp_server.get_settings", return_value=settings):
            result = await create_connector(
                name="github-primary",
                connector_type_id="github",
                credentials="ghp_super_secret_value",
                config_json={"repo": "farnalabs/modulo"},
                allowed_operations=["list_files"],
            )

        assert result["id"] == str(ci.id)
        assert result["name"] == "github-primary"
        assert result["connector_type_id"] == "github"
        assert result["visibility"] == "org"
        assert result["status"] == "created"

        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["name"] == "github-primary"
        assert call_kwargs["connector_type_id"] == "github"
        assert call_kwargs["config_json"] == {"repo": "farnalabs/modulo"}
        assert call_kwargs["allowed_operations"] == ["list_files"]
        # Credentials must be encrypted at rest, never stored as plaintext.
        assert call_kwargs["credentials_ciphertext"] != b"ghp_super_secret_value"
        assert isinstance(call_kwargs["credentials_ciphertext"], bytes)


# ---------------------------------------------------------------------------
# delete_connector
# ---------------------------------------------------------------------------


class TestDeleteConnectorErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await delete_connector(connector_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = _make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await delete_connector(connector_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await delete_connector(connector_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "connector_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.delete_connector_instance", return_value=False)
    async def test_not_found_when_connector_missing(
        self,
        mock_delete: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await delete_connector(connector_id=str(uuid.uuid4()))

        assert result["error"] == "connector_not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.delete_connector_instance")
    async def test_migration_required_on_programming_error(
        self,
        mock_delete: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_delete.side_effect = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await delete_connector(connector_id=str(uuid.uuid4()))

        assert result["error"] == "migration_required"


class TestDeleteConnectorSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.connector_instance.delete_connector_instance", return_value=True)
    async def test_returns_deleted(
        self,
        mock_delete: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())
        connector_id = str(uuid.uuid4())

        result = await delete_connector(connector_id=connector_id)

        assert result == {"status": "deleted", "connector_id": connector_id}
        mock_delete.assert_awaited_once()
