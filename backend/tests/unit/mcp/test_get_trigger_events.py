"""Unit tests for the get_trigger_events MCP tool."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import get_trigger_events

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_mock_event(
    *,
    event_id: uuid.UUID | None = None,
    trigger_id: uuid.UUID | None = None,
    trigger_type: str = "webhook",
    validation_result: str = "accepted",
    run_id: uuid.UUID | None = None,
    created_at=None,
) -> MagicMock:
    e = MagicMock()
    e.id = event_id or uuid.uuid4()
    e.trigger_id = trigger_id or uuid.uuid4()
    e.trigger_type = trigger_type
    e.validation_result = validation_result
    e.run_id = run_id
    e.created_at = created_at
    return e


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestGetTriggerEventsAuth:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_trigger_events()
        assert result["error"] == "internal_error"
        assert "revoked" in result.get("detail", "").lower() or "expired" in result.get("detail", "").lower()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    async def test_insufficient_scope(
        self,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_check_scope.side_effect = MCPAuthorizationError("insufficient_scope")
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        result = await get_trigger_events()
        assert result["error"] == "insufficient_scope"


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestGetTriggerEventsSuccess:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_all_events_when_no_filters(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = _make_mock_session()
        mock_sesh = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sesh)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        trigger_id = uuid.uuid4()
        event1 = _make_mock_event(
            event_id=uuid.uuid4(),
            trigger_id=trigger_id,
            trigger_type="webhook",
            validation_result="accepted",
            run_id=run_id,
        )
        event2 = _make_mock_event(
            event_id=uuid.uuid4(),
            trigger_id=trigger_id,
            trigger_type="cron",
            validation_result="condition_met",
            run_id=None,
        )

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [event1, event2]
        mock_sesh.execute = AsyncMock(return_value=execute_result)

        result = await get_trigger_events()

        assert result["count"] == 2
        assert len(result["events"]) == 2
        assert result["events"][0]["id"] == str(event1.id)
        assert result["events"][0]["trigger_id"] == str(trigger_id)
        assert result["events"][0]["trigger_type"] == "webhook"
        assert result["events"][0]["validation_result"] == "accepted"
        assert result["events"][0]["run_id"] == str(run_id)
        assert result["events"][1]["id"] == str(event2.id)
        assert result["events"][1]["run_id"] is None

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_filters_by_trigger_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = _make_mock_session()
        mock_sesh = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sesh)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        target_trigger_id = uuid.uuid4()
        event = _make_mock_event(trigger_id=target_trigger_id)

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [event]
        mock_sesh.execute = AsyncMock(return_value=execute_result)

        result = await get_trigger_events(trigger_id=str(target_trigger_id))

        assert result["count"] == 1
        assert result["events"][0]["trigger_id"] == str(target_trigger_id)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_filters_by_pipeline_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = _make_mock_session()
        mock_sesh = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sesh)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        pipeline_id = uuid.uuid4()
        event = _make_mock_event()

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [event]
        mock_sesh.execute = AsyncMock(return_value=execute_result)

        result = await get_trigger_events(pipeline_id=str(pipeline_id))

        assert result["count"] == 1

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_respects_limit(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = _make_mock_session()
        mock_sesh = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sesh)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        events = [_make_mock_event() for _ in range(5)]

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = events
        mock_sesh.execute = AsyncMock(return_value=execute_result)

        result = await get_trigger_events(limit=5)

        assert result["count"] == 5
        assert result["limit"] == 5

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_empty_when_no_events(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = _make_mock_session()
        mock_sesh = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_sesh)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = []
        mock_sesh.execute = AsyncMock(return_value=execute_result)

        result = await get_trigger_events(trigger_id=str(uuid.uuid4()))

        assert result["count"] == 0
        assert result["events"] == []
