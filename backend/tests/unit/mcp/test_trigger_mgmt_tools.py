"""Unit tests for the create_trigger / list_triggers MCP tools."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.api.mcp_server import create_trigger, list_triggers

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_mock_trigger(
    *,
    trigger_type: str = "manual",
    active: bool = True,
    max_concurrent_runs: int = 1,
    cron_expression: str | None = None,
    last_fired_at=None,
    created_at=None,
) -> MagicMock:
    t = MagicMock()
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.trigger_type = trigger_type
    t.active = active
    t.max_concurrent_runs = max_concurrent_runs
    t.cron_expression = cron_expression
    t.last_fired_at = last_fired_at
    t.created_at = created_at
    return t


def _make_list_result(items: list[MagicMock], *, total: int, next_cursor: str | None, has_more: bool) -> MagicMock:
    result = MagicMock()
    result.items = items
    result.total = total
    result.next_cursor = next_cursor
    result.has_more = has_more
    return result


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_add_session(trigger_id: uuid.UUID | None = None) -> AsyncMock:
    """Return a session whose ``add`` assigns the model's PK, like a real flush.

    ``Trigger.id`` has a Python-side ``default=uuid.uuid4`` that SQLAlchemy only
    applies at flush time; a mocked flush never assigns it, so the tool's
    ``str(trigger.id)`` would read ``None``. Simulating the PK assignment on
    ``add`` lets the tool build a real ``Trigger`` and return a valid id.
    """
    session = AsyncMock()
    generated_id = trigger_id or uuid.uuid4()
    session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", generated_id))
    return session


class _AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")
        _ctx_user_id.set(_PLACEHOLDER_ACCOUNT_ID)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_user_id.set(None)


# ---------------------------------------------------------------------------
# create_trigger — errors
# ---------------------------------------------------------------------------


class TestCreateTriggerErrors(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await create_trigger(pipeline_id=str(uuid.uuid4()))
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
            result = await create_trigger(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(pipeline_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"
        mock_sesh.add.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_zero_max_concurrent(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(pipeline_id=str(uuid.uuid4()), max_concurrent_runs=0)

        assert result["error"] == "validation"
        assert result["field"] == "max_concurrent_runs"
        mock_sesh.add.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_negative_spend_limit(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(pipeline_id=str(uuid.uuid4()), daily_spend_limit=-1)

        assert result["error"] == "validation"
        assert result["field"] == "daily_spend_limit"
        mock_sesh.add.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_cron_returns_invalid_cron(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        with patch("modulo.api.mcp_server.validate_cron_expression", return_value="bad expression"):
            result = await create_trigger(
                pipeline_id=str(uuid.uuid4()),
                trigger_type="cron",
                cron_expression="not a cron",
            )

        assert result["error"] == "invalid_cron"
        assert result["detail"] == "bad expression"
        mock_sesh.add.assert_not_called()


# ---------------------------------------------------------------------------
# create_trigger — success
# ---------------------------------------------------------------------------


class TestCreateTriggerSuccess(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_creates_trigger_and_returns_shape(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = _make_add_session()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(pipeline_id=str(uuid.uuid4()))

        assert result.get("error") is None
        assert result["id"] == str(uuid.UUID(result["id"]))
        assert result["pipeline_id"] == str(uuid.UUID(result["pipeline_id"]))
        assert result["trigger_type"] == "manual"
        assert result["active"] is True
        assert result["max_concurrent_runs"] == 1
        assert result["daily_spend_limit"] is None
        assert result["cron_expression"] is None
        mock_sesh.add.assert_called_once()
        mock_sesh.flush.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_creates_trigger_with_full_options(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = _make_add_session()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(
            pipeline_id=str(uuid.uuid4()),
            trigger_type="cron",
            active=False,
            max_concurrent_runs=3,
            daily_spend_limit=12.5,
            config_json={"input_template": {"foo": "bar"}},
        )

        assert result.get("error") is None
        assert result["trigger_type"] == "cron"
        assert result["active"] is False
        assert result["max_concurrent_runs"] == 3
        assert result["daily_spend_limit"] == 12.5
        assert result["cron_expression"] is None

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_cron_expression_sets_next_fire_at(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        next_fire = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
        mock_sesh = _make_add_session()
        mock_session.return_value = _make_session_context(mock_sesh)

        with (
            patch("modulo.api.mcp_server.validate_cron_expression", return_value=None) as mock_validate_cron,
            patch("modulo.api.mcp_server.compute_next_fire", return_value=next_fire) as mock_compute,
        ):
            result = await create_trigger(
                pipeline_id=str(uuid.uuid4()),
                trigger_type="cron",
                cron_expression="0 5 * * *",
            )

        mock_validate_cron.assert_called_once_with("0 5 * * *")
        mock_compute.assert_called_once_with("0 5 * * *", timezone="UTC")
        assert result.get("error") is None
        assert result["trigger_type"] == "cron"
        assert result["cron_expression"] == "0 5 * * *"


# ---------------------------------------------------------------------------
# list_triggers — errors
# ---------------------------------------------------------------------------


class TestListTriggersErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await list_triggers()
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
            result = await list_triggers()
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.list_triggers")
    async def test_migration_required_on_programming_error(
        self,
        mock_db_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_list.side_effect = ProgrammingError("SELECT ...", {}, Exception("relation does not exist"))

        result = await list_triggers()

        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.list_triggers")
    async def test_internal_error_on_generic_exception(
        self,
        mock_db_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_db_list.side_effect = RuntimeError("boom")

        result = await list_triggers()

        assert result["error"] == "internal_error"
        assert result["detail"] == "Failed to list triggers"


# ---------------------------------------------------------------------------
# list_triggers — success + filtering
# ---------------------------------------------------------------------------


class TestListTriggersSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.list_triggers")
    async def test_returns_paginated_data(
        self,
        mock_db_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        fired = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        created = datetime(2025, 12, 31, 9, 0, tzinfo=UTC)
        t1 = _make_mock_trigger(
            trigger_type="cron",
            cron_expression="0 12 * * *",
            last_fired_at=fired,
            created_at=created,
        )
        t2 = _make_mock_trigger()
        mock_db_list.return_value = _make_list_result([t1, t2], total=2, next_cursor=None, has_more=False)
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_triggers()

        assert result.get("error") is None
        assert result["total"] == 2
        assert result["next_cursor"] is None
        assert result["has_more"] is False
        assert len(result["data"]) == 2
        first = result["data"][0]
        assert first["id"] == str(t1.id)
        assert first["pipeline_id"] == str(t1.pipeline_id)
        assert first["trigger_type"] == "cron"
        assert first["active"] is True
        assert first["max_concurrent_runs"] == 1
        assert first["cron_expression"] == "0 12 * * *"
        assert first["last_fired_at"] == fired.isoformat()
        assert first["created_at"] == created.isoformat()
        assert result["data"][1]["last_fired_at"] is None
        assert result["data"][1]["created_at"] is None
        mock_db_list.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.list_triggers")
    async def test_filters_by_pipeline_id(
        self,
        mock_db_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_db_list.return_value = _make_list_result([], total=0, next_cursor=None, has_more=False)
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        pid = uuid.uuid4()
        result = await list_triggers(pipeline_id=str(pid))

        assert result.get("error") is None
        assert result["data"] == []
        mock_db_list.assert_awaited_once_with(
            mock_sesh,
            _PLACEHOLDER_ORG_ID,
            pipeline_id=pid,
            cursor=None,
            limit=20,
        )

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.list_triggers")
    async def test_limit_is_clamped_to_max_100(
        self,
        mock_db_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_db_list.return_value = _make_list_result([], total=0, next_cursor=None, has_more=False)
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_triggers(limit=999)

        assert result.get("error") is None
        mock_db_list.assert_awaited_once_with(
            mock_sesh,
            _PLACEHOLDER_ORG_ID,
            pipeline_id=None,
            cursor=None,
            limit=100,
        )

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.list_triggers")
    async def test_cursor_is_forwarded(
        self,
        mock_db_list: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_db_list.return_value = _make_list_result([], total=0, next_cursor="next-page", has_more=True)
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await list_triggers(cursor="abc123")

        assert result.get("error") is None
        assert result["next_cursor"] == "next-page"
        assert result["has_more"] is True
        mock_db_list.assert_awaited_once_with(
            mock_sesh,
            _PLACEHOLDER_ORG_ID,
            pipeline_id=None,
            cursor="abc123",
            limit=20,
        )
