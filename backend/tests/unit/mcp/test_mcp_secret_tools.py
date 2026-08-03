"""Unit tests for the create_secret / list_secrets / delete_secret MCP tools."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import create_secret, delete_secret, list_secrets

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_FERNET_KEY = "vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI="


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.fernet_key = _FERNET_KEY
    return settings


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
# create_secret
# ---------------------------------------------------------------------------


class TestCreateSecretErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_secret(key="gh_token", value="secret-value")
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
            result = await create_secret(key="gh_token", value="secret-value")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_empty_key(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await create_secret(key="   ", value="secret-value")

        assert result["error"] == "validation_failed"
        assert result["field"] == "key"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_oversized_key(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await create_secret(key="k" * 256, value="secret-value")

        assert result["error"] == "validation_failed"
        assert result["field"] == "key"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_empty_value(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await create_secret(key="gh_token", value="")

        assert result["error"] == "validation_failed"
        assert result["field"] == "value"


class TestCreateSecretSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.secrets_backend.create_secrets_backend")
    @patch("modulo.settings.get_settings")
    async def test_creates_secret_via_backend(
        self,
        mock_get_settings: MagicMock,
        mock_create_backend: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_settings.return_value = _make_settings()
        backend = AsyncMock()
        mock_create_backend.return_value = backend
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await create_secret(key="gh_token", value="super-secret")

        assert result == {"status": "created", "key": "gh_token"}
        mock_create_backend.assert_called_once()
        assert mock_create_backend.call_args.kwargs["fernet_key"] == _FERNET_KEY
        backend.set_secret.assert_awaited_once_with("gh_token", "super-secret")


# ---------------------------------------------------------------------------
# list_secrets
# ---------------------------------------------------------------------------


def _make_secret(*, key: str, created_at=None, updated_at=None) -> MagicMock:
    sec = MagicMock()
    sec.key = key
    sec.created_at = created_at
    sec.updated_at = updated_at
    return sec


class TestListSecretsErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_secrets()
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
            result = await list_secrets()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_migration_required_on_programming_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=ProgrammingError("SELECT 1", {}, Exception("no table")))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_secrets()

        assert result["error"] == "migration_required"


class TestListSecretsSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_secret_keys_with_metadata(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        created = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        updated = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        secrets = [_make_secret(key="gh_token", created_at=created, updated_at=updated)]

        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = secrets
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[list_result, count_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_secrets(search="gh_")

        assert result["total"] == 1
        assert result["secrets"] == [
            {
                "key": "gh_token",
                "created_at": created.isoformat(),
                "updated_at": updated.isoformat(),
            }
        ]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_empty_list_when_no_secrets(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = []
        count_result = MagicMock()
        count_result.scalar.return_value = 0

        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(side_effect=[list_result, count_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_secrets()

        assert result == {"secrets": [], "total": 0}


# ---------------------------------------------------------------------------
# delete_secret
# ---------------------------------------------------------------------------


class TestDeleteSecretErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await delete_secret(key="gh_token")
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
            result = await delete_secret(key="gh_token")
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_empty_key(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await delete_secret(key="")

        assert result["error"] == "validation_failed"
        assert result["field"] == "key"


class TestDeleteSecretSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.core.secrets_backend.create_secrets_backend")
    @patch("modulo.settings.get_settings")
    async def test_deletes_secret_via_backend(
        self,
        mock_get_settings: MagicMock,
        mock_create_backend: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_settings.return_value = _make_settings()
        backend = AsyncMock()
        mock_create_backend.return_value = backend
        mock_session.return_value = _make_session_context(AsyncMock())

        result = await delete_secret(key="gh_token")

        assert result == {"status": "deleted", "key": "gh_token"}
        backend.delete_secret.assert_awaited_once_with("gh_token")
