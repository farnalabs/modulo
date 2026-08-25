"""Unit tests for per-event org/token validation in MCP SSE connections.

Tests that ``validate_current_auth()`` re-checks the credential on every
handler invocation, catching mid-session revocations and OAuth family
blacklisting that occur between SSE events.
"""

import hashlib
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import (
    _ctx_auth_token,
    _ctx_auth_type,
    _ctx_org_id,
    _ctx_role,
    _ctx_user_id,
    copy_library_primitive,
    get_run_output,
    get_run_status,
    list_pending_hitl,
    list_pipelines_tool,
    resource_connectors,
    resource_hitl_gate,
    resource_model_backends,
    resource_pipeline_detail,
    resource_pipelines,
    resource_run,
    resource_schemas,
    review_hitl,
    trigger_pipeline,
    validate_current_auth,
)
from modulo.auth.api_key import ApiKeyInvalidError
from modulo.db.capacity import StorageExhaustedError
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
    from modulo.api.mcp_server import _live_role_cache

    _live_role_cache.clear()


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

    @patch("modulo.api.mcp_server.resolve_role_from_membership")
    @patch("modulo.api.mcp_server.validate_api_key")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_true_for_valid_api_key(
        self,
        mock_session: AsyncMock,
        mock_validate_api_key: AsyncMock,
        mock_resolve_role: AsyncMock,
    ) -> None:
        mock_validate_api_key.return_value = MagicMock(role="operator", id=uuid.uuid4())
        mock_resolve_role.return_value = "operator"
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")
        _ctx_user_id.set(uuid.uuid4())

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
        mock_sess = AsyncMock()
        mock_sess.execute.return_value = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sess)
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
        result = await list_pipelines_tool(limit=20)
        assert result["error"] == "auth_expired"
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
        assert result["error"] == "auth_expired"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_get_run_status_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_run_status(run_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_list_pending_hitl_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await list_pending_hitl(page=1, page_size=20)
        assert result["error"] == "auth_expired"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_review_hitl_returns_auth_error(self, mock_validate_auth: AsyncMock) -> None:
        result = await review_hitl(
            run_id=str(uuid.uuid4()),
            gate_id="gate1",
            action="claim",
        )
        assert result["error"] == "auth_expired"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_copy_library_primitive_returns_auth_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"
        mock_validate_auth.assert_called_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    @patch("modulo.api.mcp_server._session")
    async def test_get_run_output_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_run_output(run_id=str(uuid.uuid4()), node_id="node1")
        assert result["error"] == "auth_expired"
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
    async def test_resource_pipeline_detail_returns_auth_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await resource_pipeline_detail(pipeline_id=str(uuid.uuid4()))
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

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    async def test_resource_run_includes_cost_rollup(
        self,
        mock_get_child_run_rollup: AsyncMock,
        mock_get_run: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run_id = uuid.uuid4()
        run = MagicMock(
            id=run_id,
            pipeline_id=uuid.uuid4(),
            status="complete",
            trigger_type="manual",
            error_code=None,
            total_cost_usd=Decimal("0.075000"),
            cost_breakdown=None,
        )
        run.created_at = MagicMock()
        run.created_at.isoformat.return_value = "2026-06-20T14:30:00+00:00"
        mock_get_run.return_value = run
        mock_get_child_run_rollup.return_value = {run_id: (Decimal("0.125000"), 2)}

        mock_sess = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        result = await resource_run(run_id=str(run_id))

        assert "Total cost: $0.075000" in result
        assert "Child runs cost: $0.125000" in result
        assert "Child runs count: 2" in result
        assert "Aggregate cost: $0.200000" in result
        mock_get_child_run_rollup.assert_awaited_once()
        mock_get_run.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    async def test_resource_run_no_children_shows_zero_rollup(
        self,
        mock_get_child_run_rollup: AsyncMock,
        mock_get_run: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        run_id = uuid.uuid4()
        run = MagicMock(
            id=run_id,
            pipeline_id=uuid.uuid4(),
            status="complete",
            trigger_type="manual",
            error_code=None,
            total_cost_usd=None,
            cost_breakdown=None,
        )
        run.created_at = MagicMock()
        run.created_at.isoformat.return_value = "2026-06-20T14:30:00+00:00"
        mock_get_run.return_value = run
        mock_get_child_run_rollup.return_value = {}

        mock_sess = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        result = await resource_run(run_id=str(run_id))

        assert "Child runs cost: $0.000000" in result
        assert "Child runs count: 0" in result
        assert "Aggregate cost: $0.000000" in result
        assert "Total cost:" not in result
        mock_get_child_run_rollup.assert_awaited_once()
        mock_get_run.assert_awaited_once()

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

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_resource_connectors_returns_connector_uuid(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        connector_id = uuid.uuid4()
        connector = MagicMock(
            name="Slack",
            id=connector_id,
            connector_type_id="slack_webhook",
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value = [connector]
        mock_sess = AsyncMock()
        mock_sess.execute = AsyncMock(return_value=mock_result)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        result = await resource_connectors()

        assert f"id={connector_id}" in result
        assert "Slack" in result
        assert "slack_webhook" in result
        assert result.startswith("Connectors (1):")
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
        mock_page = MagicMock()
        mock_page.items = []
        mock_page.total = 0
        mock_page.next_cursor = None
        mock_page.has_more = False
        with patch("modulo.db.crud.pipeline.list_pipelines", return_value=mock_page) as mock_list:
            result = await list_pipelines_tool(limit=20)

        mock_validate_auth.assert_called_once()
        mock_list.assert_called_once()
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Integration: context vars are correctly set by McpAuthMiddleware
# ---------------------------------------------------------------------------


class TestMcpAuthMiddlewareContext:
    """McpAuthMiddleware correctly stores auth context for per-event checks."""

    def teardown_method(self) -> None:
        _reset_ctx()

    @patch("modulo.api.mcp_server.resolve_role_from_membership")
    @patch("modulo.api.mcp_server.validate_api_key")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._get_session_factory")
    async def test_middleware_sets_auth_context_for_api_key(
        self,
        mock_get_factory: MagicMock,
        mock_session: AsyncMock,
        mock_validate_api_key: AsyncMock,
        mock_resolve_role: AsyncMock,
    ) -> None:
        """Verify the middleware flow sets _ctx_auth_token and _ctx_auth_type."""
        mock_key = MagicMock(role="operator", id=uuid.uuid4())
        mock_key.run_id = None
        mock_key.name = None
        mock_validate_api_key.return_value = mock_key
        mock_resolve_role.return_value = "operator"
        mock_auth_sess = AsyncMock()
        mock_auth_sess.execute.return_value = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_auth_sess)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()
        mock_result.scalar_one_or_none.return_value.hashed_secret = hashlib.sha256(_API_KEY.encode()).hexdigest()
        mock_sess = MagicMock()
        mock_sess.begin = MagicMock()
        mock_sess.begin.return_value.__aenter__ = AsyncMock(return_value=None)
        mock_sess.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_sess.execute = AsyncMock(return_value=mock_result)
        factory_mock = MagicMock()
        factory_mock.return_value = session_cm = AsyncMock()
        session_cm.__aenter__ = AsyncMock(return_value=mock_sess)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_get_factory.return_value = factory_mock

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


async def test_trigger_pipeline_storage_exhausted_returns_error() -> None:
    """MCP trigger_pipeline must surface StorageExhaustedError as a structured error.

    The capacity gate is NOT mocked: the real ``enforce_capacity_gate`` raises
    inside ``create_run``, and the tool's ``except StorageExhaustedError`` branch
    must map it to ``{"error": "storage_exhausted"}`` instead of the generic tool
    error (FAR-426 wiring for the MCP surface).
    """
    _reset_ctx()
    _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
    _ctx_user_id.set(uuid.uuid4())
    _ctx_role.set("admin")
    _ctx_auth_type.set("api_key")

    session = _make_mock_session()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    pipeline = MagicMock()
    pipeline.owner_team_id = None
    snapshot = MagicMock()
    snapshot.id = uuid.uuid4()
    snapshot.graph_json = {"nodes": [{"id": "n"}]}

    with (
        patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("modulo.api.mcp_server._session") as mock_session,
        patch("modulo.api.mcp_server.get_pipeline", return_value=pipeline),
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new=AsyncMock(return_value=snapshot),
        ),
        patch("modulo.db.crud.run._ensure_org_not_deleted", new=AsyncMock()),
        patch(
            "modulo.db.capacity.enforce_capacity_gate",
            new=AsyncMock(
                side_effect=StorageExhaustedError(
                    "Storage capacity exhausted (99% of configured capacity, hard-stop 98%). "
                    "Export/clear old runs via Run Retention."
                )
            ),
        ),
    ):
        mock_session.return_value = session_cm
        result = await trigger_pipeline(pipeline_id=str(uuid.uuid4()))

    assert result.get("error") == "storage_exhausted"
