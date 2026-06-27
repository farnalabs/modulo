"""Unit tests for per-event org/token validation in MCP SSE connections.

Tests that ``validate_current_auth()`` re-checks the credential on every
handler invocation, catching mid-session revocations and OAuth family
blacklisting that occur between SSE events.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import (
    _ctx_auth_token,
    _ctx_auth_type,
    _ctx_org_id,
    _ctx_role,
    copy_library_primitive,
    get_run_status,
    list_pending_hitl,
    list_pipelines_tool,
    resource_connectors,
    resource_hitl_gate,
    resource_model_backends,
    resource_pipelines,
    resource_run,
    resource_schemas,
    review_hitl,
    trigger_pipeline,
    validate_current_auth,
)
from modulo.auth.api_key import ApiKeyInvalidError
from modulo.settings import Settings

_VALID_32 = "a" * 32
_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_OAUTH_TOKEN = "eyJhbGciOiJIUzI1NiJ9.oauth_access_token"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="https://modulo.example.com",
    )


def _reset_ctx() -> None:
    _ctx_org_id.set(None)
    _ctx_role.set(None)
    _ctx_auth_token.set(None)
    _ctx_auth_type.set(None)


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ---------------------------------------------------------------------------
# validate_current_auth() — direct unit tests
# ---------------------------------------------------------------------------


class TestValidateCurrentAuth:
    """Per-event auth validation logic tested in isolation."""

    def teardown_method(self) -> None:
        _reset_ctx()

    @patch("modulo.api.mcp_server.validate_api_key")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_true_for_valid_api_key(
        self,
        mock_session: AsyncMock,
        mock_validate_api_key: AsyncMock,
    ) -> None:
        mock_validate_api_key.return_value = MagicMock(role="operator", id=uuid.uuid4())
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

        result = await validate_current_auth()
        assert result is True
        mock_validate_api_key.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_api_key")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_false_for_revoked_api_key(
        self,
        mock_session: AsyncMock,
        mock_validate_api_key: AsyncMock,
    ) -> None:
        mock_validate_api_key.side_effect = ApiKeyInvalidError()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

        result = await validate_current_auth()
        assert result is False

    @patch("modulo.api.mcp_server.decode_oauth_access_token")
    @patch("modulo.api.mcp_server.check_oauth_token_family_valid")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_settings")
    async def test_returns_true_for_valid_oauth(
        self,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_check_family: AsyncMock,
        mock_decode: MagicMock,
    ) -> None:
        mock_get_settings.return_value = _make_settings()
        mock_decode.return_value = MagicMock(
            organisation_id=_PLACEHOLDER_ORG_ID,
            token_family="fam1",
            client_id="cid1",
        )
        mock_check_family.return_value = True
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_OAUTH_TOKEN)
        _ctx_auth_type.set("oauth")

        result = await validate_current_auth()
        assert result is True
        mock_decode.assert_called_once()
        mock_check_family.assert_awaited_once()

    @patch("modulo.api.mcp_server.decode_oauth_access_token")
    @patch("modulo.api.mcp_server.check_oauth_token_family_valid")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_settings")
    async def test_returns_false_for_revoked_oauth_family(
        self,
        mock_get_settings: MagicMock,
        mock_session: AsyncMock,
        mock_check_family: AsyncMock,
        mock_decode: MagicMock,
    ) -> None:
        mock_get_settings.return_value = _make_settings()
        mock_decode.return_value = MagicMock(
            organisation_id=_PLACEHOLDER_ORG_ID,
            token_family="fam1",
            client_id="cid1",
        )
        mock_check_family.return_value = False
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_OAUTH_TOKEN)
        _ctx_auth_type.set("oauth")

        result = await validate_current_auth()
        assert result is False

    async def test_returns_false_when_no_context_set(self) -> None:
        result = await validate_current_auth()
        assert result is False

    async def test_returns_false_for_unknown_auth_type(self) -> None:
        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set("some_token")
        _ctx_auth_type.set("unknown_type")

        result = await validate_current_auth()
        assert result is False


# ---------------------------------------------------------------------------
# Handler-level per-event auth enforcement
# ---------------------------------------------------------------------------


class TestHandlerPerEventAuth:
    """Every tool/resource handler calls validate_current_auth on invocation."""

    def setup_method(self) -> None:
        _reset_ctx()
        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        _reset_ctx()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_list_pipelines_returns_auth_error_on_revoked_token(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await list_pipelines_tool(page=1, page_size=20)
        assert result["error"] == "internal_error"
        assert "revoked" in result.get("detail", "").lower() or "expired" in result.get("detail", "").lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_trigger_pipeline_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_get_run_status_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_run_status(run_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_list_pending_hitl_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await list_pending_hitl(page=1, page_size=20)
        assert result["error"] == "internal_error"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_review_hitl_returns_auth_error(self, mock_validate_auth: AsyncMock) -> None:
        result = await review_hitl(
            run_id=str(uuid.uuid4()),
            gate_id="gate1",
            action="claim",
        )
        assert result["error"] == "internal_error"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_copy_library_primitive_returns_auth_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_resource_pipelines_returns_auth_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_pipelines()
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_resource_run_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_run(run_id=str(uuid.uuid4()))
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_resource_hitl_gate_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_hitl_gate(run_id=str(uuid.uuid4()), gate_id="gate1")
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_resource_schemas_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_schemas()
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_resource_connectors_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_connectors()
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_resource_model_backends_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_model_backends()
        assert "revoked" in result.lower() or "expired" in result.lower()
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_handler_proceeds_when_auth_valid(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """When validate_current_auth passes, handler continues to its logic."""
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        # patch further down the call chain
        with patch("modulo.api.mcp_server.list_pipelines") as mock_list:
            mock_list.return_value = MagicMock(items=[], total=0, page=1, page_size=20)
            result = await list_pipelines_tool(page=1, page_size=20)

        mock_validate_auth.assert_called_once()
        mock_list.assert_called_once()
        assert result["total"] == 0
        assert result["pipelines"] == []


# ---------------------------------------------------------------------------
# Integration: context vars are correctly set by McpAuthMiddleware
# ---------------------------------------------------------------------------


class TestMcpAuthMiddlewareContext:
    """McpAuthMiddleware correctly stores auth context for per-event checks."""

    def teardown_method(self) -> None:
        _reset_ctx()

    @patch("modulo.api.mcp_server.validate_api_key")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._get_session_factory")
    async def test_middleware_sets_auth_context_for_api_key(
        self,
        mock_get_factory: MagicMock,
        mock_session: AsyncMock,
        mock_validate_api_key: AsyncMock,
    ) -> None:
        """Verify the middleware flow sets _ctx_auth_token and _ctx_auth_type."""
        mock_validate_api_key.return_value = MagicMock(role="operator", id=uuid.uuid4())
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm
        mock_get_factory.return_value = MagicMock()

        _reset_ctx()

        from starlette.requests import Request
        from starlette.responses import PlainTextResponse

        from modulo.api.mcp_server import McpAuthMiddleware

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/tools/call",
            "headers": [
                (b"authorization", f"Bearer {_API_KEY}".encode()),
                (b"host", b"localhost"),
            ],
            "query_string": b"",
            "scheme": "http",
            "client": ("127.0.0.1", 8000),
            "server": ("localhost", 8000),
        }

        async def noop_call_next(request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        middleware = McpAuthMiddleware(MagicMock())
        await middleware.dispatch(Request(scope), noop_call_next)

        assert _ctx_auth_token.get(None) == _API_KEY
        assert _ctx_auth_type.get(None) == "api_key"
