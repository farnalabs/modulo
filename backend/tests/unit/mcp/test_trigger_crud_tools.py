"""Unit tests for the get_trigger / update_trigger / delete_trigger MCP tools."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import create_trigger, delete_trigger, get_trigger, update_trigger
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _full_streak_status(**overrides: object) -> dict[str, object]:
    """FAR-251 — the SAME uniform 6-key shape the REST serializers surface."""
    status: dict[str, object] = {
        "enabled": True,
        "streak": 4,
        "threshold": 5,
        "state": "ok",
        "deactivated_reason": None,
        "last_outcomes": [
            {
                "run_id": "run-1",
                "classification": "no_delivery",
                "reason": "no_work",
                "completed_at": "2026-08-01T00:00:00Z",
            }
        ],
    }
    status.update(overrides)
    return status


_BASE_STREAK_STATUS = {
    "enabled": False,
    "streak": 0,
    "threshold": 0,
    "state": "unconfigured",
    "deactivated_reason": None,
    "last_outcomes": [],
}


def test_mcp_reuses_the_rest_streak_builder() -> None:
    """FAR-251 — the MCP trigger tools and the REST serializers MUST share ONE
    streak-status builder (no divergence). The MCP tools import the routes'
    ``_streak_status_for`` directly, so the two surfaces are the same function
    object by construction."""
    import modulo.api.mcp_server as mcp
    import modulo.api.routes.triggers as routes

    assert mcp._streak_status_for is routes._streak_status_for


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


def _make_add_session(trigger_id: uuid.UUID | None = None) -> AsyncMock:
    """Return a session whose ``add`` assigns the model's PK, like a real flush
    (``Trigger.id`` has a Python-side ``default=uuid.uuid4`` SQLAlchemy only
    applies at flush time)."""
    session = AsyncMock()
    generated_id = trigger_id or uuid.uuid4()
    session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", generated_id))
    return session


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


class _TrackingSessionCtx:
    """A session context that tracks whether the ``async with _session`` scope
    is active — proves the MCP streak reads run INSIDE the RLS session block
    (mirrors the REST ``_TrackingBegin`` test for the FAR-191 list fix)."""

    active = False

    def __init__(self, session: AsyncMock) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncMock:
        _TrackingSessionCtx.active = True
        return self.session

    async def __aexit__(self, *args: object) -> bool:
        _TrackingSessionCtx.active = False
        return False


# ---------------------------------------------------------------------------
# get_trigger — streak_status surfacing (FAR-251)
# ---------------------------------------------------------------------------


class TestGetTriggerStreakStatus(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_trigger_includes_streak_status_for_ongoing(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """The MCP get_trigger surfaces the SAME streak_status shape the REST
        detail serializer returns for an ongoing trigger."""
        trigger = _make_mock_trigger(trigger_type="ongoing", active=False)
        streak_status = _full_streak_status(streak=5, state="deactivated", deactivated_reason="no_delivery_streak")
        mock_sesh = AsyncMock()
        trigger_result = _make_execute_result(trigger)
        owner_team_result = _make_execute_result(None)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        mock_sesh.execute = AsyncMock(side_effect=[trigger_result, owner_team_result, count_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        with patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ) as get_status:
            result = await get_trigger(trigger_id=str(trigger.id))

        assert result.get("error") is None
        assert result["trigger_type"] == "ongoing"
        assert result["in_flight"] == 2
        assert result["streak_status"] == streak_status
        get_status.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_trigger_streak_status_uniform_base_for_non_ongoing(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A non-ongoing trigger still gets the uniform 6-key streak_status shape
        ({enabled: false, state: 'unconfigured'}, zero queries) — the reader
        short-circuits before issuing any streak-engine query."""
        trigger = _make_mock_trigger(trigger_type="cron")
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await get_trigger(trigger_id=str(trigger.id))

        assert result.get("error") is None
        assert result["streak_status"] == _BASE_STREAK_STATUS

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_trigger_deactivated_trigger_surfaces_state_and_reason(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """An auto-deactivated trigger surfaces state='deactivated' plus the
        deactivation reason (here 'no_delivery_streak') so MCP operators can see
        the deactivated badge + reason without hitting the REST API."""
        trigger = _make_mock_trigger(trigger_type="ongoing", active=False)
        streak_status = _full_streak_status(
            streak=5,
            threshold=5,
            state="deactivated",
            deactivated_reason="no_delivery_streak",
            last_outcomes=[
                {
                    "run_id": "run-9",
                    "classification": "no_delivery",
                    "reason": "no_work",
                    "completed_at": "2026-08-02T00:00:00Z",
                }
            ],
        )
        mock_sesh = AsyncMock()
        trigger_result = _make_execute_result(trigger)
        owner_team_result = _make_execute_result(None)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 5
        mock_sesh.execute = AsyncMock(side_effect=[trigger_result, owner_team_result, count_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        with patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ):
            result = await get_trigger(trigger_id=str(trigger.id))

        assert result.get("error") is None
        assert result["active"] is False
        assert result["streak_status"]["state"] == "deactivated"
        assert result["streak_status"]["deactivated_reason"] == "no_delivery_streak"
        assert result["streak_status"] == streak_status

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_get_trigger_streak_read_runs_inside_rls_session_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """The streak read must happen INSIDE the _session block (RLS scope) —
        SET LOCAL app.organisation_id is transaction-scoped, so a post-commit
        read would see zero rows and silently report state 'ok'."""
        trigger = _make_mock_trigger(trigger_type="ongoing", active=False)
        streak_status = _full_streak_status(streak=5, state="deactivated", deactivated_reason="no_delivery_streak")
        mock_sesh = AsyncMock()
        trigger_result = _make_execute_result(trigger)
        owner_team_result = _make_execute_result(None)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 5
        mock_sesh.execute = AsyncMock(side_effect=[trigger_result, owner_team_result, count_result])
        mock_session.return_value = _TrackingSessionCtx(mock_sesh)

        async def _streak_status(*args: object, **kwargs: object) -> dict[str, object]:
            assert _TrackingSessionCtx.active, "streak read must happen inside the RLS session scope"
            return streak_status

        with patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            side_effect=_streak_status,
        ):
            result = await get_trigger(trigger_id=str(trigger.id))

        assert result.get("error") is None
        assert result["streak_status"] == streak_status
        assert _TrackingSessionCtx.active is False, "the session scope must have closed after the response"


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

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_masked_secret_round_trip_preserves_stored_value(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(config_json={"hmac_secret": "real-secret"})
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), config_json={"hmac_secret": SENSITIVE_VALUE_MASK})

        assert result.get("error") is None
        assert trigger.config_json["hmac_secret"] == "real-secret"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_new_secret_value_is_written_through(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(config_json={"hmac_secret": "old-secret"})
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), config_json={"hmac_secret": "new-secret"})

        assert result.get("error") is None
        assert trigger.config_json["hmac_secret"] == "new-secret"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_null_secret_clears_key(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(config_json={"hmac_secret": "real-secret"})
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(trigger_id=str(trigger.id), config_json={"hmac_secret": None})

        assert result.get("error") is None
        assert "hmac_secret" not in trigger.config_json

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_masked_secret_preserved_while_other_keys_merge(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        trigger = _make_mock_trigger(config_json={"hmac_secret": "real-secret", "work_item_ref_paths": [".agents"]})
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await update_trigger(
            trigger_id=str(trigger.id),
            config_json={"hmac_secret": SENSITIVE_VALUE_MASK, "work_item_ref_paths": ["backend", "frontend"]},
        )

        assert result.get("error") is None
        assert trigger.config_json["hmac_secret"] == "real-secret"
        assert trigger.config_json["work_item_ref_paths"] == ["backend", "frontend"]


# ---------------------------------------------------------------------------
# update_trigger — streak_status surfacing (FAR-251)
# ---------------------------------------------------------------------------


class TestUpdateTriggerStreakStatus(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("operator")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_update_trigger_includes_streak_status(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """The MCP update_trigger response surfaces the refreshed streak_status
        exactly as the REST update serializer does."""
        trigger = _make_mock_trigger(trigger_type="ongoing", daily_spend_limit=Decimal("25.00"))
        streak_status = _full_streak_status(streak=0, state="ok", deactivated_reason=None, last_outcomes=[])
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(10))
        mock_session.return_value = _make_session_context(mock_sesh)

        with patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ) as get_status:
            result = await update_trigger(trigger_id=str(trigger.id), max_concurrent_runs=4)

        assert result.get("error") is None
        assert result["streak_status"] == streak_status
        get_status.assert_awaited_once()


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
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(None))
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await delete_trigger(trigger_id=str(uuid.uuid4()))

        assert result == {"error": "not_found", "detail": "Trigger not found"}
        mock_soft_delete.assert_not_awaited()

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
        mock_sesh.execute = AsyncMock(return_value=_make_execute_result(trigger))
        mock_session.return_value = _make_session_context(mock_sesh)
        with patch("modulo.api.mcp_server._pipeline_owner_team_id", AsyncMock(return_value=None)):
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


class TestCreateTriggerStreakStatus(_AuthContext):
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
    async def test_create_trigger_includes_streak_status(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """The MCP create_trigger response surfaces the created trigger's
        streak_status (the anchored streak=0 / state=ok baseline for a fresh
        ongoing trigger) exactly as the REST create serializer does."""
        streak_status = _full_streak_status(streak=0, state="ok", deactivated_reason=None, last_outcomes=[])
        mock_sesh = _make_add_session()
        mock_sesh.get = AsyncMock(return_value=_pipeline_cap(10))
        mock_session.return_value = _make_session_context(mock_sesh)

        with patch(
            "modulo.api.routes.triggers.get_trigger_streak_status",
            new_callable=AsyncMock,
            return_value=streak_status,
        ) as get_status:
            result = await create_trigger(
                pipeline_id=str(uuid.uuid4()),
                trigger_type="ongoing",
                max_concurrent_runs=3,
                daily_spend_limit=25.0,
            )

        assert result.get("error") is None
        assert result["trigger_type"] == "ongoing"
        assert result["streak_status"] == streak_status
        get_status.assert_awaited_once()


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
        owner_team_result = _make_execute_result(None)
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        mock_sesh.execute = AsyncMock(side_effect=[trigger_result, owner_team_result, count_result])
        mock_session.return_value = _make_session_context(mock_sesh)

        result = await get_trigger(trigger_id=str(trigger.id))

        assert result["trigger_type"] == "ongoing"
        assert result["in_flight"] == 2
