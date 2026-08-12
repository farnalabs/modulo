"""Unit tests for the get_trigger / update_trigger / delete_trigger MCP tools."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import create_trigger, delete_trigger, get_trigger, update_trigger

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_mock_trigger(
    *,
    trigger_id: uuid.UUID | None = None,
    trigger_type: str = "manual",
    active: bool = True,
    max_concurrent_runs: int = 1,
    daily_spend_limit: Decimal | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    last_fired_at=None,
    next_fire_at=None,
    config_json: dict | None = None,
) -> MagicMock:
    t = MagicMock()
    t.id = trigger_id or uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.trigger_type = trigger_type
    t.active = active
    t.max_concurrent_runs = max_concurrent_runs
    t.daily_spend_limit = daily_spend_limit
    t.cron_expression = cron_expression
    t.cron_timezone = cron_timezone
    t.last_fired_at = last_fired_at
    t.next_fire_at = next_fire_at
    t.config_json = config_json or {}
    return t


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_execute_result(trigger: MagicMock | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = trigger
    return result


class _AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

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


# ---------------------------------------------------------------------------
# Common error cases — get_trigger
# ---------------------------------------------------------------------------


class TestGetTriggerErrors(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_trigger(trigger_id=str(uuid.uuid4()))
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
            result = await get_trigger(trigger_id=str(uuid.uuid4()))
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

        result = await get_trigger(trigger_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "trigger_id"
        mock_sesh.execute.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_not_found_when_trigger_missing(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(None))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await get_trigger(trigger_id=str(uuid.uuid4()))

        assert result == {"error": "not_found", "detail": "Trigger not found"}


# ---------------------------------------------------------------------------
# get_trigger — success
# ---------------------------------------------------------------------------


class TestGetTriggerSuccess(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_returns_full_trigger_shape(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        fired = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        next_fire = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        trigger = _make_mock_trigger(
            trigger_type="cron",
            max_concurrent_runs=3,
            daily_spend_limit=Decimal("12.50"),
            cron_expression="0 12 * * *",
            cron_timezone="UTC",
            last_fired_at=fired,
            next_fire_at=next_fire,
            config_json={"input_template": {"foo": "bar"}},
        )
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await get_trigger(trigger_id=str(trigger.id))

        assert result["id"] == str(trigger.id)
        assert result["pipeline_id"] == str(trigger.pipeline_id)
        assert result["trigger_type"] == "cron"
        assert result["active"] is True
        assert result["max_concurrent_runs"] == 3
        assert result["daily_spend_limit"] == 12.50
        assert result["config_json"] == {"input_template": {"foo": "bar"}}
        assert result["cron_expression"] == "0 12 * * *"
        assert result["cron_timezone"] == "UTC"
        assert result["last_fired_at"] == fired.isoformat()
        assert result["next_fire_at"] == next_fire.isoformat()
        assert result["input_template"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# update_trigger — errors
# ---------------------------------------------------------------------------


class TestUpdateTriggerErrors(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await update_trigger(trigger_id=str(uuid.uuid4()))
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
            result = await update_trigger(trigger_id=str(uuid.uuid4()))
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

        result = await update_trigger(trigger_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "trigger_id"
        mock_sesh.execute.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_not_found_when_trigger_missing(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(None))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(uuid.uuid4()), active=False)

        assert result == {"error": "not_found", "detail": "Trigger not found"}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_negative_spend_limit(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(uuid.uuid4()), daily_spend_limit=-1)

        assert result["error"] == "validation"
        assert result["field"] == "daily_spend_limit"
        mock_sesh.execute.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_validation_error_for_zero_max_concurrent(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(uuid.uuid4()), max_concurrent_runs=0)

        assert result["error"] == "validation"
        assert result["field"] == "max_concurrent_runs"
        mock_sesh.execute.assert_not_called()


# ---------------------------------------------------------------------------
# update_trigger — cron + success
# ---------------------------------------------------------------------------


class TestUpdateTriggerSuccess(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_cron_change_recomputes_next_fire_at(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(trigger_type="cron", cron_expression="0 0 * * *", cron_timezone="UTC")
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        next_fire = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
        with (
            patch("modulo.api.mcp_server.validate_cron_expression", return_value=None) as mock_validate_cron,
            patch("modulo.api.mcp_server.compute_next_fire", return_value=next_fire) as mock_compute,
        ):
            result = await update_trigger(trigger_id=str(trigger.id), cron_expression="*/5 * * * *")

        mock_validate_cron.assert_called_once_with("*/5 * * * *", "UTC")
        mock_compute.assert_called_once_with("*/5 * * * *", timezone="UTC")
        assert trigger.cron_expression == "*/5 * * * *"
        assert trigger.next_fire_at == next_fire
        assert result.get("error") is None
        assert result["cron_expression"] == "*/5 * * * *"
        assert result["next_fire_at"] == next_fire.isoformat()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_cron_config_on_non_cron_trigger_returns_validation_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(trigger_type="manual")
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), cron_expression="*/5 * * * *")

        assert result == {"error": "validation", "detail": "Only cron triggers can have cron configuration"}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_cron_returns_invalid_cron(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(trigger_type="cron")
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        with patch("modulo.api.mcp_server.validate_cron_expression", return_value="bad expression"):
            result = await update_trigger(trigger_id=str(trigger.id), cron_expression="not a cron")

        assert result["error"] == "invalid_cron"
        assert result["detail"] == "bad expression"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_daily_spend_limit_set(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger()
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), daily_spend_limit=10.5)

        assert trigger.daily_spend_limit == Decimal("10.5")
        assert result["daily_spend_limit"] == 10.5

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_daily_spend_limit_cleared(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(daily_spend_limit=Decimal("20.00"))
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), clear_daily_spend_limit=True)

        assert trigger.daily_spend_limit is None
        assert result["daily_spend_limit"] is None

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_active_and_config_json_updated(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger()
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        new_config = {"input_template": {"x": 1}}
        result = await update_trigger(trigger_id=str(trigger.id), active=False, config_json=new_config)

        assert trigger.active is False
        assert trigger.config_json == new_config
        assert result["active"] is False
        assert result["config_json"] == new_config


# ---------------------------------------------------------------------------
# delete_trigger
# ---------------------------------------------------------------------------


class TestDeleteTrigger(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await delete_trigger(trigger_id=str(uuid.uuid4()))
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
            result = await delete_trigger(trigger_id=str(uuid.uuid4()))
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

        result = await delete_trigger(trigger_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert result["field"] == "trigger_id"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.soft_delete_trigger", return_value=None)
    async def test_not_found_when_trigger_missing(
        self,
        mock_soft_delete: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await delete_trigger(trigger_id=str(uuid.uuid4()))

        assert result == {"error": "not_found", "detail": "Trigger not found"}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.trigger.soft_delete_trigger")
    async def test_success_returns_deleted(
        self,
        mock_soft_delete: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger()
        mock_soft_delete.return_value = trigger
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await delete_trigger(trigger_id=str(trigger.id))

        assert result == {"id": str(trigger.id), "deleted": True}
        mock_soft_delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# create_trigger — ongoing guards (FAR-158)
# ---------------------------------------------------------------------------


def _pipeline_cap(cap: int):
    from types import SimpleNamespace

    return SimpleNamespace(max_concurrent_runs=cap, is_break_glass=False)


class TestCreateTriggerOngoing(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role, _ctx_user_id

        _ctx_role.set("operator")
        _ctx_user_id.set(uuid.uuid4())

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_user_id

        _ctx_user_id.set(None)
        super().teardown_method()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_ongoing_without_spend_limit_rejected(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(10))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(
            pipeline_id=str(uuid.uuid4()),
            trigger_type="ongoing",
            max_concurrent_runs=3,
            daily_spend_limit=None,
        )

        assert result["error"] == "validation"
        assert "daily_spend_limit" in result["detail"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_ongoing_valid_accepted_with_next_fire_at(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(10))
        mock_sesh.add = MagicMock()
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(
            pipeline_id=str(uuid.uuid4()),
            trigger_type="ongoing",
            max_concurrent_runs=3,
            daily_spend_limit=25.0,
            config_json={"scan_interval_seconds": 120},
        )

        assert result.get("error") is None
        assert result["trigger_type"] == "ongoing"
        assert result["max_concurrent_runs"] == 3
        assert result["daily_spend_limit"] == 25.0
        added_trigger = mock_sesh.add.call_args.args[0]
        assert added_trigger.next_fire_at is not None, "a fresh ongoing trigger must fire on the first tick"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_ongoing_target_above_pipeline_cap_rejected(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(5))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await create_trigger(
            pipeline_id=str(uuid.uuid4()),
            trigger_type="ongoing",
            max_concurrent_runs=20,
            daily_spend_limit=25.0,
        )

        assert result["error"] == "validation"
        assert "cannot exceed" in result["detail"]


# ---------------------------------------------------------------------------
# update_trigger / get_trigger — ongoing guards (FAR-158)
# ---------------------------------------------------------------------------


class TestUpdateTriggerOngoing(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_clear_spend_limit_on_ongoing_rejected(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from decimal import Decimal

        trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal("25.00"))
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), clear_daily_spend_limit=True)

        assert result["error"] == "validation"
        assert "clearing it is not allowed" in result["detail"]
        mock_sesh.flush.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_raising_target_above_pipeline_cap_rejected(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from decimal import Decimal

        trigger = _make_mock_trigger(trigger_type="ongoing", max_concurrent_runs=3, daily_spend_limit=Decimal("25.00"))
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(5))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), max_concurrent_runs=20)

        assert result["error"] == "validation"
        assert "cannot exceed" in result["detail"]
        mock_sesh.flush.assert_not_called()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_ongoing_edit_recomputes_next_fire_at(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from decimal import Decimal

        trigger = _make_mock_trigger(trigger_type="ongoing", max_concurrent_runs=2, daily_spend_limit=Decimal("25.00"))
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(10))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), max_concurrent_runs=4)

        assert result.get("error") is None
        assert trigger.max_concurrent_runs == 4
        assert trigger.next_fire_at is not None, "a target change must reset next_fire_at"
        assert result["next_fire_at"] is not None


class TestGetTriggerOngoing(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_trigger_includes_in_flight(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from decimal import Decimal

        trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal("25.00"))
        mock_sesh = AsyncMock()
        trigger_result = _make_execute_result(trigger)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        mock_sesh.execute = AsyncMock(side_effect=[trigger_result, count_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await get_trigger(trigger_id=str(trigger.id))

        assert result["trigger_type"] == "ongoing"
        assert result["in_flight"] == 2
