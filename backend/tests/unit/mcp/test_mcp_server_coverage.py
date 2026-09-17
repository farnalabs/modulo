"""Tests to raise coverage of api/mcp_server.py uncovered by the mcp/ suite.

Targets pure helpers, serialisation, validation, and error-path branches
that the existing mcp/ test files do not reach. All tests use mocks only —
no DB, no Docker.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

import modulo.api.mcp_server as ms
from modulo.api.mcp_server import (
    _analytics_deep_link,
    _apply_node_connector_binding,
    _assert_admin_scope,
    _assert_create_eval_definition_params,
    _assert_eval_type,
    _assert_failure_behaviour,
    _assert_pass_threshold,
    _assert_update_eval_definition_params,
    _build_analytics_params,
    _check_agent_tool_scope,
    _clamp_mcp_number,
    _collect_eval_definition_updates,
    _create_manual_run,
    _detect_masked_fields,
    _extract_node_id_from_key_name,
    _iso_or_none,
    _mcp_run_item,
    _parse_analytics_enums,
    _parse_analytics_ids,
    _parse_basic_auth_header,
    _parse_eval_ref_ids,
    _parse_mcp_datetime,
    _parse_uuid_param,
    _quantize_mcp_cost_rollup,
    _resolve_run_node_output,
    _run_status_base,
    _run_status_detail,
    _run_status_node,
    _sanitize_cost_breakdown,
    _sanitize_cost_breakdown_entry,
    _serialize_edges,
    _serialize_run_evals,
    _team_scope_error,
    _team_scope_error_str,
    _team_scoped_key_mismatch,
    _tool_auth_error,
    _tool_db_shell,
    _tool_error,
    _trigger_pipeline_validate_id,
    _validate_sandbox_nodes,
    _validate_trigger_numbers,
)
from modulo.core.mcp.scope_validator import MCPAuthorizationError
from tests.unit.mcp.helpers import API_KEY, ORG_ID, USER_ID, make_session_context

_TEAM = uuid.UUID("00000000-0000-0000-0000-000000000009")


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class _Ctx:
    """Set up and tear down MCP context vars for a test."""

    def setup_method(self) -> None:
        ms._ctx_org_id.set(ORG_ID)
        ms._ctx_role.set("operator")
        ms._ctx_user_id.set(USER_ID)
        ms._ctx_key_id.set(USER_ID)
        ms._ctx_auth_token.set(API_KEY)
        ms._ctx_auth_type.set("api_key")
        ms._ctx_team_id.set(None)
        ms._ctx_node_allowed_tools.set(None)

    def teardown_method(self) -> None:
        for var in (
            ms._ctx_org_id,
            ms._ctx_role,
            ms._ctx_user_id,
            ms._ctx_key_id,
            ms._ctx_auth_token,
            ms._ctx_auth_type,
            ms._ctx_team_id,
        ):
            var.set(None)
        ms._ctx_node_allowed_tools.set(None)


class _AdminCtx(_Ctx):
    def setup_method(self) -> None:
        super().setup_method()
        ms._ctx_role.set("admin")


# ─── Serialisation helpers ──────────────────────────────────────────


class TestSerializeEdges:
    def test_serializes_edges(self) -> None:
        e1 = SimpleNamespace(
            id=uuid.uuid4(), source_node_id=uuid.uuid4(), target_node_id=uuid.uuid4(), edge_type="normal"
        )
        result = _serialize_edges([e1])
        assert len(result) == 1
        assert result[0]["id"] == str(e1.id)
        assert result[0]["edge_type"] == "normal"

    def test_empty_list(self) -> None:
        assert not _serialize_edges([])


class TestSerializeRunEvals:
    def test_with_node_id(self) -> None:
        ev = SimpleNamespace(
            id=uuid.uuid4(),
            eval_id=uuid.uuid4(),
            node_id=uuid.uuid4(),
            passed=True,
            score=0.9,
            detail="ok",
            evaluated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = _serialize_run_evals([ev])
        assert len(result) == 1
        assert result[0]["node_id"] == str(ev.node_id)
        assert result[0]["evaluated_at"] is not None

    def test_without_node_id(self) -> None:
        ev = SimpleNamespace(
            id=uuid.uuid4(),
            eval_id=uuid.uuid4(),
            node_id=None,
            passed=False,
            score=0.0,
            detail=None,
            evaluated_at=None,
        )
        result = _serialize_run_evals([ev])
        assert result[0]["node_id"] is None
        assert result[0]["evaluated_at"] is None

    def test_empty_list(self) -> None:
        assert not _serialize_run_evals([])


class TestIsoOrNone:
    def test_with_value(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        assert _iso_or_none(dt) == dt.isoformat()

    def test_with_none(self) -> None:
        assert _iso_or_none(None) is None

    def test_with_falsy(self) -> None:
        assert _iso_or_none(0) is None
        assert _iso_or_none("") is None


# ─── Run status formatting ──────────────────────────────────────────


class TestRunStatusBase:
    def test_full_run(self) -> None:
        run = SimpleNamespace(
            id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            status="completed",
            trigger_type="manual",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            error_code=None,
            error_detail=None,
        )
        result = _run_status_base(run)
        assert result["status"] == "completed"
        assert result["started_at"] is not None
        assert result["completed_at"] is not None

    def test_no_started_no_completed(self) -> None:
        run = SimpleNamespace(
            id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            status="pending",
            trigger_type="manual",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            error_code=None,
            error_detail=None,
        )
        result = _run_status_base(run)
        assert "started_at" not in result
        assert "completed_at" not in result

    def test_error_code_and_detail(self) -> None:
        run = SimpleNamespace(
            id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            status="failed",
            trigger_type="manual",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            error_code="sandbox_node_failed",
            error_detail="agent timed out",
        )
        with (
            patch.object(ms, "map_legacy_code", return_value="sandbox_node_failed"),
            patch.object(ms, "present_error", return_value=("sandbox_node_failed", "agent timed out")),
        ):
            result = _run_status_base(run)
        assert "error_code" in result
        assert "error_detail" in result

    def test_error_code_only_no_detail(self) -> None:
        run = SimpleNamespace(
            id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            status="failed",
            trigger_type="manual",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            error_code="stall_detected",
            error_detail=None,
        )
        with (
            patch.object(ms, "map_legacy_code", return_value="stall_detected"),
            patch.object(ms, "present_error", return_value=("stall_detected", "")),
        ):
            result = _run_status_base(run)
        assert "error_code" in result
        assert "error_detail" not in result


class TestRunStatusNode:
    def test_completed_node(self) -> None:
        node = _run_status_node(
            "n1",
            {"n1": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "cost_usd": 0.01}},
            {"n1": {"result": "ok"}},
            {},
        )
        assert node["status"] == "completed"
        assert node["input_tokens"] == 10

    def test_failed_telemetry(self) -> None:
        node = _run_status_node("n1", {}, {}, {"n1": {"status": "failed"}})
        assert node["status"] == "failed"

    def test_processed_telemetry(self) -> None:
        node = _run_status_node("n1", {}, {}, {"n1": {"status": "running"}})
        assert node["status"] == "processed"

    def test_no_data_node(self) -> None:
        node = _run_status_node("n1", {}, {}, {})
        assert node["status"] == "processed"

    def test_usage_not_dict(self) -> None:
        node = _run_status_node("n1", {"n1": "bad"}, {}, {})
        assert node["input_tokens"] == 0

    def test_model_cost_display_usd(self) -> None:
        node = _run_status_node(
            "n1",
            {"n1": {"input_tokens": 1, "output_tokens": 1, "model_cost_display_usd": 0.5}},
            {},
            {},
        )
        assert node["model_cost_display_usd"] == 0.5

    def test_total_tokens_fallback(self) -> None:
        node = _run_status_node("n1", {"n1": {"input_tokens": 5, "output_tokens": 3}}, {}, {})
        assert node["total_tokens"] == 8


class TestRunStatusDetail:
    def test_with_cost_breakdown(self) -> None:
        run = SimpleNamespace(
            node_token_usage={"n1": {"input_tokens": 1}},
            cost_breakdown=[{"component": "llm", "amount_usd": 0.5}],
        )
        blobs = SimpleNamespace(outputs={"n1": {"result": "ok"}}, telemetry={})
        result = _run_status_detail(run, blobs)
        assert "nodes" in result
        assert "cost_breakdown" in result
        assert len(result["nodes"]) == 1

    def test_no_cost_breakdown(self) -> None:
        run = SimpleNamespace(node_token_usage=None, cost_breakdown=None)
        blobs = SimpleNamespace(outputs={}, telemetry=None)
        result = _run_status_detail(run, blobs)
        assert "cost_breakdown" not in result

    def test_telemetry_not_dict(self) -> None:
        run = SimpleNamespace(node_token_usage={}, cost_breakdown=None)
        blobs = SimpleNamespace(outputs={}, telemetry="not-a-dict")
        result = _run_status_detail(run, blobs)
        assert not result["nodes"]


# ─── Run item formatting ────────────────────────────────────────────


class TestMcpRunItem:
    def test_with_error(self) -> None:
        r = SimpleNamespace(
            id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            status="failed",
            trigger_type="manual",
            run_number=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            error_code="stall_detected",
            error_detail="timeout",
            total_cost_usd=None,
        )
        with patch.object(ms, "present_error", return_value=("stall_detected", "timeout")):
            result = _mcp_run_item(r, {})
        assert result["error_code"] == "stall_detected"

    def test_with_cost(self) -> None:
        r = SimpleNamespace(
            id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            status="completed",
            trigger_type="manual",
            run_number=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            error_code=None,
            error_detail=None,
            total_cost_usd=Decimal("0.123456"),
        )
        with patch.object(ms, "present_error", return_value=(None, None)):
            result = _mcp_run_item(r, {})
        assert result["total_cost_usd"] == pytest.approx(0.123456)
        assert result["child_runs_cost_usd"] == 0.0
        assert result["child_runs_count"] == 0

    def test_with_child_rollup(self) -> None:
        rid = uuid.uuid4()
        r = SimpleNamespace(
            id=rid,
            pipeline_id=uuid.uuid4(),
            status="completed",
            trigger_type="cron",
            run_number=5,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            error_code=None,
            error_detail=None,
            total_cost_usd=Decimal("0.10"),
        )
        rollup = {rid: (Decimal("0.05"), 3)}
        with patch.object(ms, "present_error", return_value=(None, None)):
            result = _mcp_run_item(r, rollup)
        assert result["child_runs_count"] == 3
        assert result["child_runs_cost_usd"] == pytest.approx(0.05, abs=1e-6)


# ─── Quantize cost rollup ──────────────────────────────────────────


class TestQuantizeCostRollup:
    def test_normalises(self) -> None:
        assert _quantize_mcp_cost_rollup(Decimal("0.123456789")) == Decimal("0.123457")

    def test_zero(self) -> None:
        assert _quantize_mcp_cost_rollup(Decimal(0)) == Decimal("0.000000")

    def test_large(self) -> None:
        assert _quantize_mcp_cost_rollup(Decimal("999.9999999")) == Decimal("1000.000000")


# ─── Resolve run node output ────────────────────────────────────────


class TestResolveRunNodeOutput:
    def test_found_in_outputs(self) -> None:
        outputs = {"n1": {"result": "ok"}}
        result = _resolve_run_node_output(outputs, {}, "n1")
        assert result == {"result": "ok"}

    def test_not_in_outputs_fallback_telemetry(self) -> None:
        telemetry = {"n1": {"status": "failed", "summary": "timeout"}}
        result = _resolve_run_node_output({}, telemetry, "n1")
        assert result is not None
        assert result.get("status") == "failed"

    def test_not_found_anywhere(self) -> None:
        result = _resolve_run_node_output({}, {}, "missing")
        assert result is None


# ─── Detect masked fields ──────────────────────────────────────────


class TestDetectMaskedFields:
    def test_detects_bullets(self) -> None:
        masked = {"token": "•••••", "name": "hello"}
        result = _detect_masked_fields(masked)
        assert "token" in result
        assert "name" not in result

    def test_non_dict(self) -> None:
        assert not _detect_masked_fields("not-a-dict")

    def test_empty_dict(self) -> None:
        assert not _detect_masked_fields({})


# ─── Validate trigger numbers ──────────────────────────────────────


class TestValidateTriggerNumbers:
    def test_valid(self) -> None:
        assert _validate_trigger_numbers(5, 10.0) is None

    def test_none_values(self) -> None:
        assert _validate_trigger_numbers(None, None) is None

    def test_max_concurrent_too_low(self) -> None:
        err = _validate_trigger_numbers(0, None)
        assert err is not None
        assert err["field"] == "max_concurrent_runs"

    def test_daily_spend_negative(self) -> None:
        err = _validate_trigger_numbers(None, -1.0)
        assert err is not None
        assert err["field"] == "daily_spend_limit"


# ─── Parse UUID param ──────────────────────────────────────────────


class TestParseUuidParam:
    def test_valid(self) -> None:
        uid = uuid.uuid4()
        val, err = _parse_uuid_param(str(uid), "id")
        assert val == uid
        assert err is None

    def test_invalid(self) -> None:
        val, err = _parse_uuid_param("not-a-uuid", "id")
        assert val is None
        assert err is not None
        assert err["field"] == "id"


# ─── Extract node id from key name ─────────────────────────────────


class TestExtractNodeIdFromKeyName:
    def test_valid_run_scoped_key(self) -> None:
        name = "run:abc-123:node:node-42"
        assert _extract_node_id_from_key_name(name) == "node-42"

    def test_no_node_marker(self) -> None:
        assert _extract_node_id_from_key_name("mk_foo_bar") is None

    def test_empty_name(self) -> None:
        assert _extract_node_id_from_key_name(None) is None
        assert _extract_node_id_from_key_name("") is None

    def test_node_marker_empty_suffix(self) -> None:
        assert _extract_node_id_from_key_name("run:abc:node:") is None


# ─── Team scoped key mismatch ──────────────────────────────────────


class TestTeamScopedKeyMismatch(_Ctx):
    def test_no_team_boundary(self) -> None:
        assert _team_scoped_key_mismatch(uuid.uuid4()) is False

    def test_same_team(self) -> None:
        ms._ctx_team_id.set(_TEAM)
        assert _team_scoped_key_mismatch(_TEAM) is False

    def test_different_team(self) -> None:
        ms._ctx_team_id.set(_TEAM)
        other = uuid.uuid4()
        assert _team_scoped_key_mismatch(other) is True

    def test_org_resource_with_team_key(self) -> None:
        ms._ctx_team_id.set(_TEAM)
        assert _team_scoped_key_mismatch(None) is False


class TestTeamScopeError:
    def test_error_shape(self) -> None:
        ms._ctx_team_id.set(_TEAM)
        err = _team_scope_error("pipeline", "p-123")
        assert err["error"] == "team_boundary_violation"
        assert str(_TEAM) in err["detail"]

    def test_error_str(self) -> None:
        ms._ctx_team_id.set(_TEAM)
        s = _team_scope_error_str("run", "r-456")
        assert "team_boundary_violation" in s


# ─── Tool error helpers ────────────────────────────────────────────


class TestToolErrorHelpers:
    def test_tool_error(self) -> None:
        result = _tool_error("something broke")
        assert result["error"] == "internal_error"
        assert result["detail"] == "something broke"

    def test_tool_auth_error(self) -> None:
        result = _tool_auth_error("token expired")
        assert result["error"] == "auth_expired"
        assert result["detail"] == "token expired"


# ─── Trigger pipeline validate id ──────────────────────────────────


class TestTriggerPipelineValidateId:
    def test_valid(self) -> None:
        uid = uuid.uuid4()
        val, err = _trigger_pipeline_validate_id(str(uid))
        assert val == uid
        assert err is None

    def test_invalid(self) -> None:
        val, err = _trigger_pipeline_validate_id("bad")
        assert val is None
        assert err is not None


# ─── Parse basic auth header ───────────────────────────────────────


class TestParseBasicAuthHeader:
    def _make_request(self, header: str) -> MagicMock:
        req = MagicMock()
        req.headers = {"Authorization": header}
        return req

    def test_no_auth(self) -> None:
        req = self._make_request("")
        delta, err = _parse_basic_auth_header(req, {})
        assert delta == {}
        assert err is None

    def test_non_basic(self) -> None:
        req = self._make_request("Bearer token")
        delta, err = _parse_basic_auth_header(req, {})
        assert delta == {}
        assert err is None

    def test_malformed_base64(self) -> None:
        req = self._make_request("Basic !!!invalid-base64!!!")
        delta, err = _parse_basic_auth_header(req, {})
        assert delta == {}
        assert err is not None
        assert err.status_code == 400

    def test_valid_basic(self) -> None:
        import base64

        creds = base64.b64encode(b"client1:secret1").decode()
        req = self._make_request(f"Basic {creds}")
        delta, err = _parse_basic_auth_header(req, {})
        assert delta["client_id"] == "client1"
        assert delta["client_secret"] == "secret1"
        assert err is None

    def test_does_not_override_existing(self) -> None:
        import base64

        creds = base64.b64encode(b"client1:secret1").decode()
        req = self._make_request(f"Basic {creds}")
        delta, err = _parse_basic_auth_header(req, {"client_id": "existing"})
        assert "client_id" not in delta
        assert delta["client_secret"] == "secret1"
        assert err is None


# ─── Parse MCP datetime ────────────────────────────────────────────


class TestParseMcpDatetime:
    def test_iso_datetime(self) -> None:
        result = _parse_mcp_datetime("2026-01-01T12:00:00", "date_from")
        assert result.year == 2026
        assert result.hour == 12

    def test_bare_date(self) -> None:
        result = _parse_mcp_datetime("2026-01-01", "date_from")
        assert result.year == 2026
        assert result.hour == 0

    def test_invalid(self) -> None:
        from modulo.core.analytics.service import AnalyticsValidationError

        with pytest.raises(AnalyticsValidationError, match="invalid date"):
            _parse_mcp_datetime("not-a-date", "date_from")


# ─── Parse analytics enums ─────────────────────────────────────────


class TestParseAnalyticsEnums:
    def test_valid(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy

        grp, _tt, _st, _dim, err = _parse_analytics_enums("day", None, None)
        assert grp == AnalyticsGroupBy.DAY
        assert err is None

    def test_invalid(self) -> None:
        grp, _tt, _st, _dim, err = _parse_analytics_enums("bogus", None, None)
        assert grp is None
        assert err is not None

    def test_with_trigger_type(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy, AnalyticsTriggerType

        grp, tt, _st, _dim, err = _parse_analytics_enums("day", "cron", None)
        assert grp == AnalyticsGroupBy.DAY
        assert tt == AnalyticsTriggerType.CRON
        assert err is None

    def test_with_dimension(self) -> None:
        from modulo.core.analytics.builder import AnalyticsDimension, AnalyticsGroupBy

        grp, _tt, _st, dim, err = _parse_analytics_enums("day", None, None, dimension="trigger_type")
        assert grp == AnalyticsGroupBy.DAY
        assert dim == AnalyticsDimension.TRIGGER_TYPE
        assert err is None

    def test_invalid_dimension(self) -> None:
        grp, _tt, _st, _dim, err = _parse_analytics_enums("day", None, None, dimension="bogus")
        assert grp is None
        assert err is not None


# ─── Parse analytics ids ──────────────────────────────────────────


class TestParseAnalyticsIds:
    def test_valid_pids(self) -> None:
        uid = uuid.uuid4()
        pids, _fid, err = _parse_analytics_ids([str(uid)], None)
        assert len(pids) == 1
        assert err is None

    def test_invalid_pid(self) -> None:
        pids, _fid, err = _parse_analytics_ids(["bad-uuid"], None)
        assert pids == ()
        assert err is not None

    def test_valid_folder(self) -> None:
        uid = uuid.uuid4()
        _pids, fid, err = _parse_analytics_ids(None, str(uid))
        assert fid == uid
        assert err is None

    def test_invalid_folder(self) -> None:
        _pids, fid, err = _parse_analytics_ids(None, "bad-uuid")
        assert fid is None
        assert err is not None

    def test_empty_pids(self) -> None:
        pids, fid, err = _parse_analytics_ids([], None)
        assert pids == ()
        assert fid is None
        assert err is None


# ─── Assert eval helpers ───────────────────────────────────────────


class TestAssertEvalType:
    def test_valid(self) -> None:
        assert _assert_eval_type("llm_judge") is None
        assert _assert_eval_type("regex") is None

    def test_invalid(self) -> None:
        err = _assert_eval_type("bogus")
        assert err is not None
        assert err["error"] == "invalid_eval_type"


class TestAssertFailureBehaviour:
    def test_valid(self) -> None:
        assert _assert_failure_behaviour("warn") is None
        assert _assert_failure_behaviour("block") is None

    def test_invalid(self) -> None:
        err = _assert_failure_behaviour("crash")
        assert err is not None
        assert err["error"] == "invalid_failure_behaviour"


class TestAssertPassThreshold:
    def test_valid(self) -> None:
        assert _assert_pass_threshold(0.5) is None
        assert _assert_pass_threshold(None) is None
        assert _assert_pass_threshold(0.0) is None
        assert _assert_pass_threshold(1.0) is None

    def test_below_zero(self) -> None:
        err = _assert_pass_threshold(-0.1)
        assert err is not None

    def test_above_one(self) -> None:
        err = _assert_pass_threshold(1.5)
        assert err is not None


class TestAssertAdminScope(_Ctx):
    def test_admin_passes(self) -> None:
        ms._ctx_role.set("admin")
        _assert_admin_scope("create")  # should not raise

    def test_operator_denied(self) -> None:
        ms._ctx_role.set("operator")
        with pytest.raises(MCPAuthorizationError):
            _assert_admin_scope("create")

    def test_runner_denied(self) -> None:
        ms._ctx_role.set("runner")
        with pytest.raises(MCPAuthorizationError):
            _assert_admin_scope("delete")


# ─── Check agent tool scope ────────────────────────────────────────


class TestCheckAgentToolScope(_Ctx):
    def test_allowed_tool(self) -> None:
        with patch.object(ms, "check_tool_scope") as mock_check:
            _check_agent_tool_scope("list_pipelines")
            mock_check.assert_called_once()

    def test_with_action(self) -> None:
        with patch.object(ms, "check_tool_scope") as mock_check:
            _check_agent_tool_scope("review_hitl", action="approve")
            call_args = mock_check.call_args
            assert call_args.kwargs["action"] == "approve"


# ─── _tool_db_shell exception ladder ────────────────────────────────


class TestToolDbShell:
    @pytest.mark.asyncio
    async def test_mcp_auth_error(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise MCPAuthorizationError("denied")

        result = await handler()
        assert result["error"] == "insufficient_scope"

    @pytest.mark.asyncio
    async def test_starlette_http_with_handle(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail", handle_http_exception=True)
        async def handler() -> dict[str, Any]:
            raise StarletteHTTPException(status_code=400, detail="bad input")

        result = await handler()
        assert result["error"] == "validation_failed"
        assert "bad input" in result["detail"]

    @pytest.mark.asyncio
    async def test_starlette_http_without_handle(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise StarletteHTTPException(status_code=400, detail="bad input")

        result = await handler()
        assert result["error"] == "internal_error"

    @pytest.mark.asyncio
    async def test_integrity_error_with_detail(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail="constraint violated: {orig}", fallback="fail")
        async def handler() -> dict[str, Any]:
            raise IntegrityError("stmt", {}, Exception("unique_violation"))

        result = await handler()
        assert result["error"] == "conflict"

    @pytest.mark.asyncio
    async def test_integrity_error_no_detail(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise IntegrityError("stmt", {}, Exception("orig"))

        result = await handler()
        assert result["error"] == "database_unavailable"

    @pytest.mark.asyncio
    async def test_programming_error(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise ProgrammingError("stmt", {}, Exception("column missing"))

        result = await handler()
        assert result["error"] == "migration_required"

    @pytest.mark.asyncio
    async def test_sqlalchemy_error(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise SQLAlchemyError("down")

        result = await handler()
        assert result["error"] == "database_unavailable"

    @pytest.mark.asyncio
    async def test_generic_exception(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise RuntimeError("oops")

        result = await handler()
        assert result["error"] == "internal_error"

    @pytest.mark.asyncio
    async def test_integrity_error_db_errors_to_fallback(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail", db_errors_to_fallback=True)
        async def handler() -> dict[str, Any]:
            raise IntegrityError("stmt", {}, Exception("orig"))

        result = await handler()
        assert result["error"] == "internal_error"

    @pytest.mark.asyncio
    async def test_sqlalchemy_error_db_errors_to_fallback(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail", db_errors_to_fallback=True)
        async def handler() -> dict[str, Any]:
            raise SQLAlchemyError("down")

        result = await handler()
        assert result["error"] == "internal_error"


# ─── Validate sandbox nodes ────────────────────────────────────────


class TestValidateSandboxNodes:
    def test_non_sandbox_nodes_pass(self) -> None:
        nodes = [{"node_type": "agent"}]
        assert _validate_sandbox_nodes(nodes) is None

    def test_empty_list(self) -> None:
        assert _validate_sandbox_nodes([]) is None

    def test_sandbox_mode_error(self) -> None:
        nodes = [{"node_type": "sandbox_agent"}]
        with patch(
            "modulo.core.pipeline_engine.sandbox_mode._validate_sandbox_mode_config",
            side_effect=ValueError("bad mode"),
        ):
            err = _validate_sandbox_nodes(nodes)
            assert err is not None
            assert err["error"] == "validation_failed"

    def test_sandbox_jinja_error(self) -> None:
        nodes = [{"node_type": "sandbox_agent"}]
        with (
            patch("modulo.core.pipeline_engine.sandbox_mode._validate_sandbox_mode_config"),
            patch(
                "modulo.core.pipeline_engine.sandbox_mode.validate_sandbox_agent_command_jinja",
                return_value="bad jinja",
            ),
        ):
            err = _validate_sandbox_nodes(nodes)
            assert err is not None
            assert "bad jinja" in err["detail"]

    def test_sandbox_managed_inputs_error(self) -> None:
        nodes = [{"node_type": "sandbox_agent"}]
        with (
            patch("modulo.core.pipeline_engine.sandbox_mode._validate_sandbox_mode_config"),
            patch("modulo.core.pipeline_engine.sandbox_mode.validate_sandbox_agent_command_jinja", return_value=None),
            patch(
                "modulo.core.pipeline_engine.sandbox_mode._validate_sandbox_managed_inputs_config",
                side_effect=ValueError("bad inputs"),
            ),
        ):
            err = _validate_sandbox_nodes(nodes)
            assert err is not None
            assert err["error"] == "validation_failed"


# ─── Apply node connector binding ──────────────────────────────────


class TestApplyNodeConnectorBinding:
    def test_node_found(self) -> None:
        nid = uuid.uuid4()
        nodes = [{"id": str(nid), "node_type": "agent"}]
        pipeline = SimpleNamespace(graph_nodes_json=nodes)
        result = _apply_node_connector_binding(pipeline, nid, str(nid), "github", "conn-1")
        assert result is None
        assert nodes[0]["connector_binding"]["type"] == "github"

    def test_node_not_found(self) -> None:
        pipeline = SimpleNamespace(graph_nodes_json=[{"id": str(uuid.uuid4())}])
        result = _apply_node_connector_binding(pipeline, uuid.uuid4(), "missing", "github", "conn-1")
        assert result is not None
        assert result["error"] == "node_not_found"

    def test_no_graph_nodes(self) -> None:
        pipeline = SimpleNamespace(graph_nodes_json=None)
        result = _apply_node_connector_binding(pipeline, uuid.uuid4(), "missing", "github", "conn-1")
        assert result is not None


# ─── Parse eval ref ids ────────────────────────────────────────────


class TestParseEvalRefIds:
    def test_valid_no_node(self) -> None:
        uid = uuid.uuid4()
        primary, node, err = _parse_eval_ref_ids(str(uid), "eval_id", None)
        assert primary == uid
        assert node is None
        assert err is None

    def test_valid_with_node(self) -> None:
        pid = uuid.uuid4()
        nid = uuid.uuid4()
        primary, node, err = _parse_eval_ref_ids(str(pid), "eval_id", str(nid))
        assert primary == pid
        assert node == nid
        assert err is None

    def test_invalid_primary(self) -> None:
        primary, _node, err = _parse_eval_ref_ids("bad", "eval_id", None)
        assert primary is None
        assert err is not None

    def test_invalid_node(self) -> None:
        pid = uuid.uuid4()
        primary, node, err = _parse_eval_ref_ids(str(pid), "eval_id", "bad")
        assert primary == pid
        assert node is None
        assert err is not None


# ─── Analytics deep link ───────────────────────────────────────────


class TestAnalyticsDeepLink:
    def test_basic_link(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy
        from modulo.core.analytics.service import AnalyticsParams

        params = AnalyticsParams(
            group_by=AnalyticsGroupBy.DAY,
            auto_granularity=False,
            dimension=None,
            trigger_type=None,
            status=None,
            pipeline_ids=(),
            team_id=None,
            error_code=None,
            folder_id=None,
            date_from=None,
            date_to=None,
            limit=100,
        )
        result = {"group_by": "day", "date_from": "2026-01-01", "date_to": "2026-01-31"}
        link = _analytics_deep_link(result, params)
        assert link.startswith("/analytics?")
        assert "group_by=day" in link

    def test_with_filters(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy, AnalyticsStatus, AnalyticsTriggerType
        from modulo.core.analytics.service import AnalyticsParams

        params = AnalyticsParams(
            group_by=AnalyticsGroupBy.DAY,
            auto_granularity=False,
            dimension=None,
            trigger_type=AnalyticsTriggerType.CRON,
            status=AnalyticsStatus.COMPLETE,
            pipeline_ids=(uuid.uuid4(),),
            team_id=None,
            error_code="stall",
            folder_id=uuid.uuid4(),
            date_from=None,
            date_to=None,
            limit=100,
        )
        result = {"group_by": "day"}
        link = _analytics_deep_link(result, params)
        assert "trigger_type=cron" in link
        assert "status=complete" in link
        assert "error_code=stall" in link


# ─── Build analytics params ────────────────────────────────────────


class TestBuildAnalyticsParams:
    def test_basic(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy

        params = _build_analytics_params(
            group_by=AnalyticsGroupBy.DAY,
            auto_granularity=False,
            trigger_type=None,
            status=None,
            dimension=None,
            pipeline_ids=(),
            folder_id=None,
            error_code=None,
            date_from=None,
            date_to=None,
            limit=100,
        )
        assert params.group_by == AnalyticsGroupBy.DAY
        assert params.limit == 100

    def test_limit_clamping(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy

        params = _build_analytics_params(
            group_by=AnalyticsGroupBy.DAY,
            auto_granularity=False,
            trigger_type=None,
            status=None,
            dimension=None,
            pipeline_ids=(),
            folder_id=None,
            error_code=None,
            date_from=None,
            date_to=None,
            limit=0,
        )
        assert params.limit == 1  # clamped to min

    def test_limit_max(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy

        params = _build_analytics_params(
            group_by=AnalyticsGroupBy.DAY,
            auto_granularity=False,
            trigger_type=None,
            status=None,
            dimension=None,
            pipeline_ids=(),
            folder_id=None,
            error_code=None,
            date_from=None,
            date_to=None,
            limit=9999,
        )
        assert params.limit == 1000  # clamped to max

    def test_date_from(self) -> None:
        from modulo.core.analytics.builder import AnalyticsGroupBy

        params = _build_analytics_params(
            group_by=AnalyticsGroupBy.DAY,
            auto_granularity=False,
            trigger_type=None,
            status=None,
            dimension=None,
            pipeline_ids=(),
            folder_id=None,
            error_code=None,
            date_from="2026-01-01",
            date_to=None,
            limit=100,
        )
        assert params.date_from is not None


# ─── Validate trigger numbers ──────────────────────────────────────


class TestValidateTriggerNumbersExtended:
    def test_zero_concurrent(self) -> None:
        err = _validate_trigger_numbers(0, 0.0)
        assert err is not None
        assert err["field"] == "max_concurrent_runs"

    def test_negative_spend(self) -> None:
        err = _validate_trigger_numbers(1, -5.0)
        assert err is not None
        assert err["field"] == "daily_spend_limit"


# ─── Validate principal live (success path) ─────────────────────────


class TestValidatePrincipalLiveSuccess(_Ctx):
    async def test_live_role_applied(self) -> None:
        from modulo.api.mcp_server import _validate_principal_live

        principal = MagicMock()
        principal.organisation_id = ORG_ID
        principal.account_id = USER_ID
        with patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value="admin")):
            assert await _validate_principal_live(API_KEY, principal) is True
        assert ms._ctx_role.get() == "admin"


# ─── Create manual run ────────────────────────────────────────────


class TestCreateManualRun(_Ctx):
    async def test_pipeline_not_found(self) -> None:
        session = _mock_session()
        with (
            patch.object(ms, "_session", return_value=make_session_context(session)),
            patch("modulo.api.mcp_server.get_pipeline", new_callable=AsyncMock, return_value=None),
        ):
            run_id, _thread_id, err = await _create_manual_run(
                session,
                ORG_ID,
                uuid.uuid4(),
                str(uuid.uuid4()),
                {},
            )
        assert run_id is None
        assert err is not None
        assert err["error"] == "pipeline_not_found"

    async def test_snapshot_failed(self) -> None:
        session = _mock_session()
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        with (
            patch.object(ms, "_session", return_value=make_session_context(session)),
            patch("modulo.api.mcp_server.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.mcp_server._team_scoped_key_mismatch", return_value=False),
            patch(
                "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            _run_id, _thread_id, err = await _create_manual_run(
                session,
                ORG_ID,
                uuid.uuid4(),
                str(uuid.uuid4()),
                {},
            )
        assert err is not None
        assert err["error"] == "snapshot_failed"

    async def test_empty_graph(self) -> None:
        session = _mock_session()
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        snapshot = MagicMock()
        snapshot.graph_json = {"nodes": []}
        with (
            patch.object(ms, "_session", return_value=make_session_context(session)),
            patch("modulo.api.mcp_server.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.mcp_server._team_scoped_key_mismatch", return_value=False),
            patch(
                "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=snapshot,
            ),
        ):
            _run_id, _thread_id, err = await _create_manual_run(
                session,
                ORG_ID,
                uuid.uuid4(),
                str(uuid.uuid4()),
                {},
            )
        assert err is not None
        assert err["error"] == "validation_failed"


# ─── Sanitize string helpers ───────────────────────────────────────


class TestSanitizeString:
    def test_strips_control_chars(self) -> None:
        from modulo.api.mcp_server import _sanitize_mcp_string

        result = _sanitize_mcp_string("hello\x00\x01\x1f world")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "hello" in result
        assert "world" in result

    def test_preserves_tab(self) -> None:
        from modulo.api.mcp_server import _sanitize_mcp_string

        result = _sanitize_mcp_string("hello\tworld")
        assert "\t" in result

    def test_truncates_at_256(self) -> None:
        from modulo.api.mcp_server import _sanitize_mcp_string

        result = _sanitize_mcp_string("x" * 300)
        assert len(result) == 256


class TestClampMcpNumber:
    def test_inf_returns_clamp(self) -> None:
        result = _clamp_mcp_number(float("inf"))
        from modulo.api.mcp_server import RAW_REPORTED_DISPLAY_CLAMP

        assert result == float(RAW_REPORTED_DISPLAY_CLAMP)

    def test_nan_returns_clamp(self) -> None:
        result = _clamp_mcp_number(float("nan"))
        from modulo.api.mcp_server import RAW_REPORTED_DISPLAY_CLAMP

        assert result == float(RAW_REPORTED_DISPLAY_CLAMP)

    def test_negative_inf_returns_clamp(self) -> None:
        result = _clamp_mcp_number(float("-inf"))
        from modulo.api.mcp_server import RAW_REPORTED_DISPLAY_CLAMP

        assert result == float(RAW_REPORTED_DISPLAY_CLAMP)


class TestSanitizeCostBreakdown:
    def test_non_list_returns_empty(self) -> None:
        assert not _sanitize_cost_breakdown("not-a-list")
        assert not _sanitize_cost_breakdown(42)
        assert not _sanitize_cost_breakdown(None)

    def test_non_dict_entries_skipped(self) -> None:
        result = _sanitize_cost_breakdown(["not-a-dict", 42, None])
        assert not result

    def test_mixed_entries(self) -> None:
        result = _sanitize_cost_breakdown(
            [
                "skip",
                {"component": "llm", "amount_usd": 0.5, "unknown_key": "gone"},
            ]
        )
        assert len(result) == 1
        assert result[0]["component"] == "llm"
        assert "unknown_key" not in result[0]

    def test_basis_sanitized(self) -> None:
        result = _sanitize_cost_breakdown(
            [
                {
                    "component": "test",
                    "basis": {"raw_reported": 1e300},
                }
            ]
        )
        assert result[0]["basis"]["raw_reported"] == 1e6


class TestSanitizeCostBreakdownEntry:
    def test_all_branches(self) -> None:
        entry = {
            "component": "x" * 300,
            "display_name": "test",
            "amount_usd": 0.5,
            "formula_applied": "cost = x",
            "source": datetime(2026, 1, 1, tzinfo=UTC),
            "missing_self_report": True,
            "error": "something went wrong",
            "unknown_key": "dropped",
        }
        out = _sanitize_cost_breakdown_entry(entry)
        assert out["component"] == "x" * 256
        assert "unknown_key" not in out
        assert out["amount_usd"] == 0.5


# ─── Eval definition validation ───────────────────────────────────


class TestAssertCreateEvalDefinitionParams:
    def test_empty_name(self) -> None:
        err = _assert_create_eval_definition_params("", "llm_judge", "warn", None)
        assert err is not None
        assert err["error"] == "invalid_name"

    def test_whitespace_name(self) -> None:
        err = _assert_create_eval_definition_params("   ", "llm_judge", "warn", None)
        assert err is not None
        assert err["error"] == "invalid_name"

    def test_name_too_long(self) -> None:
        err = _assert_create_eval_definition_params("x" * 256, "llm_judge", "warn", None)
        assert err is not None
        assert err["error"] == "invalid_name"

    def test_bad_eval_type(self) -> None:
        err = _assert_create_eval_definition_params("test", "bogus", "warn", None)
        assert err is not None
        assert err["error"] == "invalid_eval_type"

    def test_bad_failure_behaviour(self) -> None:
        err = _assert_create_eval_definition_params("test", "llm_judge", "crash", None)
        assert err is not None
        assert err["error"] == "invalid_failure_behaviour"

    def test_bad_pass_threshold(self) -> None:
        err = _assert_create_eval_definition_params("test", "llm_judge", "warn", 2.0)
        assert err is not None
        assert err["error"] == "invalid_pass_threshold"

    def test_all_valid(self) -> None:
        assert _assert_create_eval_definition_params("my eval", "llm_judge", "warn", 0.5) is None


class TestAssertUpdateEvalDefinitionParams:
    def test_bad_eval_type(self) -> None:
        err = _assert_update_eval_definition_params("bogus", None, None, None)
        assert err is not None

    def test_bad_failure_behaviour(self) -> None:
        err = _assert_update_eval_definition_params(None, "crash", None, None)
        assert err is not None

    def test_bad_pass_threshold(self) -> None:
        err = _assert_update_eval_definition_params(None, None, -1.0, None)
        assert err is not None

    def test_empty_name(self) -> None:
        err = _assert_update_eval_definition_params(None, None, None, "  ")
        assert err is not None
        assert err["error"] == "invalid_name"

    def test_name_too_long(self) -> None:
        err = _assert_update_eval_definition_params(None, None, None, "x" * 256)
        assert err is not None
        assert err["error"] == "invalid_name"

    def test_all_valid(self) -> None:
        assert _assert_update_eval_definition_params("llm_judge", "block", 0.8, "new name") is None

    def test_all_none(self) -> None:
        assert _assert_update_eval_definition_params(None, None, None, None) is None


class TestCollectEvalDefinitionUpdates:
    def test_all_provided(self) -> None:
        nid = uuid.uuid4()
        updates = _collect_eval_definition_updates(
            node_id=str(nid),
            nid=nid,
            name="test",
            eval_type="llm_judge",
            config_json={"key": "val"},
            failure_behaviour="warn",
            pass_threshold=0.5,
            suite_id="suite-1",
        )
        assert updates["node_id"] == nid
        assert updates["name"] == "test"
        assert updates["eval_type"] == "llm_judge"
        assert updates["failure_behaviour"] == "warn"
        assert updates["pass_threshold"] == 0.5
        assert updates["suite_id"] == "suite-1"

    def test_none_values_skipped(self) -> None:
        updates = _collect_eval_definition_updates(
            node_id=None,
            nid=None,
            name=None,
            eval_type=None,
            config_json=None,
            failure_behaviour=None,
            pass_threshold=None,
            suite_id=None,
        )
        assert not updates


# ─── More _create_manual_run error paths ──────────────────────────


class TestCreateManualRunTeamScope(_Ctx):
    async def test_team_scope_mismatch(self) -> None:
        session = _mock_session()
        pipeline = MagicMock()
        pipeline.owner_team_id = uuid.uuid4()
        ms._ctx_team_id.set(_TEAM)
        with (
            patch.object(ms, "_session", return_value=make_session_context(session)),
            patch(
                "modulo.api.mcp_server.get_pipeline",
                new_callable=AsyncMock,
                return_value=pipeline,
            ),
            patch(
                "modulo.api.mcp_server._team_scoped_key_mismatch",
                return_value=True,
            ),
        ):
            _run_id, _thread_id, err = await _create_manual_run(
                session,
                ORG_ID,
                uuid.uuid4(),
                str(uuid.uuid4()),
                {},
            )
        assert err is not None
        assert err["error"] == "team_boundary_violation"
