"""Unit tests for FAR-1161 accountability-owner MCP tools.

Covers ``set_pipeline_owners`` end to end (error envelopes + success), the
owner params of the ``create_pipeline``/``list_pipelines`` tools, and the
``_parse_optional_uuid`` / ``_owner_id_str`` helpers. The context/session
scaffolding is reused from the MCP coverage-gaps suite.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException as FastAPIHTTPException
from sqlalchemy.exc import ProgrammingError

import modulo.api.mcp_server as ms
from modulo.core.mcp.scope_validator import MCPAuthorizationError
from tests.unit.api.test_mcp_server_coverage_gaps import _AuthContext

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


class TestParseOptionalUuid:
    def test_none_passes_through(self) -> None:
        assert ms._parse_optional_uuid(None, "business_owner_id") == (None, None)

    def test_valid_uuid(self) -> None:
        value, err = ms._parse_optional_uuid(str(_OWNER_ID), "business_owner_id")
        assert value == _OWNER_ID
        assert err is None

    def test_invalid_uuid(self) -> None:
        value, err = ms._parse_optional_uuid("not-a-uuid", "business_owner_id")
        assert value is None
        assert err is not None
        assert err["error"] == "invalid_id"
        assert err["field"] == "business_owner_id"


class TestOwnerIdStr:
    def test_uuid_serialises(self) -> None:
        assert ms._owner_id_str(_OWNER_ID) == str(_OWNER_ID)

    def test_none_serialises_to_none(self) -> None:
        assert ms._owner_id_str(None) is None

    def test_non_uuid_attribute_is_none(self) -> None:
        assert ms._owner_id_str(MagicMock()) is None


class TestSetPipelineOwners(_AuthContext):
    @pytest.fixture(autouse=True)
    def _auth_ok(self):
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            yield

    async def test_auth_failure_returns_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id=None
            )
        assert result["error"] == "auth_expired"

    async def test_invalid_pipeline_id(self) -> None:
        result = await ms.set_pipeline_owners(pipeline_id="nope", business_owner_id=None, reliability_owner_id=None)
        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"

    async def test_invalid_business_owner_id(self) -> None:
        result = await ms.set_pipeline_owners(
            pipeline_id=str(_PIPELINE_ID), business_owner_id="nope", reliability_owner_id=None
        )
        assert result["error"] == "invalid_id"
        assert result["field"] == "business_owner_id"

    async def test_invalid_reliability_owner_id(self) -> None:
        result = await ms.set_pipeline_owners(
            pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id="nope"
        )
        assert result["error"] == "invalid_id"
        assert result["field"] == "reliability_owner_id"

    async def test_team_boundary_violation(self) -> None:
        ms._ctx_team_id.set(_TEAM_A)
        try:
            with patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_B)):
                result = await ms.set_pipeline_owners(
                    pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id=None
                )
        finally:
            ms._ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"

    async def test_pipeline_not_found(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(return_value=None)),
        ):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id=None
            )
        assert result == {"error": "pipeline_not_found", "pipeline_id": str(_PIPELINE_ID)}

    async def test_success_returns_stored_owners(self) -> None:
        pipeline = MagicMock()
        pipeline.business_owner_id = _OWNER_ID
        pipeline.reliability_owner_id = None
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(return_value=pipeline)) as update,
        ):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=str(_OWNER_ID), reliability_owner_id=None
            )
        assert result == {
            "pipeline_id": str(_PIPELINE_ID),
            "business_owner_id": str(_OWNER_ID),
            "reliability_owner_id": None,
        }
        assert update.await_args.kwargs["org_id"] == _ORG_ID
        assert update.await_args.kwargs["account_id"] == _USER_ID
        assert update.await_args.args[2] == {
            "business_owner_id": _OWNER_ID,
            "reliability_owner_id": None,
        }

    async def test_eligibility_422_maps_to_validation_failed(self) -> None:
        exc = FastAPIHTTPException(status_code=422, detail="business_owner_id: account x does not exist")
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(side_effect=exc)),
        ):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=str(_OWNER_ID), reliability_owner_id=None
            )
        assert result["error"] == "validation_failed"
        assert "does not exist" in result["detail"]

    async def test_scope_denial_maps_to_insufficient_scope(self) -> None:
        with patch.object(ms, "_check_agent_tool_scope", side_effect=MCPAuthorizationError("no set_pipeline_owners")):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id=None
            )
        assert result["error"] == "insufficient_scope"

    async def test_programming_error_maps_to_migration_required(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.pipeline.update_pipeline",
                new=AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception())),
            ),
        ):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id=None
            )
        assert result["error"] == "migration_required"

    async def test_unexpected_error_maps_to_internal_error(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = await ms.set_pipeline_owners(
                pipeline_id=str(_PIPELINE_ID), business_owner_id=None, reliability_owner_id=None
            )
        assert result["error"] == "internal_error"


class TestCreatePipelineOwnerParams(_AuthContext):
    @pytest.fixture(autouse=True)
    def _auth_ok(self):
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            yield

    async def test_invalid_owner_id_rejected_before_db(self) -> None:
        result = await ms.create_pipeline(name="p", business_owner_id="not-a-uuid")
        assert result["error"] == "invalid_id"
        assert result["field"] == "business_owner_id"

    async def test_invalid_reliability_owner_id_rejected(self) -> None:
        result = await ms.create_pipeline(name="p", reliability_owner_id="not-a-uuid")
        assert result["error"] == "invalid_id"
        assert result["field"] == "reliability_owner_id"

    async def test_success_serialises_owner_ids(self) -> None:
        pipeline = MagicMock()
        pipeline.id = _PIPELINE_ID
        pipeline.name = "p"
        pipeline.description = None
        pipeline.visibility = "org"
        pipeline.max_concurrent_runs = 5
        pipeline.default_autonomy_level = "manual_approval"
        pipeline.circuit_breaker_threshold = None
        pipeline.max_autonomy_level = None
        pipeline.business_owner_id = _OWNER_ID
        pipeline.reliability_owner_id = None
        pipeline.created_at = None
        with patch("modulo.db.crud.pipeline.create_pipeline", new=AsyncMock(return_value=pipeline)) as create:
            result = await ms.create_pipeline(name="p", business_owner_id=str(_OWNER_ID), reliability_owner_id=None)
        assert result["business_owner_id"] == str(_OWNER_ID)
        assert result["reliability_owner_id"] is None
        assert create.await_args.kwargs["business_owner_id"] == _OWNER_ID

    async def test_eligibility_422_maps_to_validation_failed(self) -> None:
        exc = FastAPIHTTPException(status_code=422, detail="reliability_owner_id: account x is deactivated")
        with patch("modulo.db.crud.pipeline.create_pipeline", new=AsyncMock(side_effect=exc)):
            result = await ms.create_pipeline(name="p", reliability_owner_id=str(_OWNER_ID))
        assert result["error"] == "validation_failed"
        assert "deactivated" in result["detail"]

    async def test_scope_denial_maps_to_insufficient_scope(self) -> None:
        with patch.object(ms, "_check_agent_tool_scope", side_effect=MCPAuthorizationError("no create_pipeline")):
            result = await ms.create_pipeline(name="p")
        assert result["error"] == "insufficient_scope"

    async def test_programming_error_maps_to_migration_required(self) -> None:
        with patch(
            "modulo.db.crud.pipeline.create_pipeline",
            new=AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception())),
        ):
            result = await ms.create_pipeline(name="p")
        assert result["error"] == "migration_required"

    async def test_unexpected_error_maps_to_internal_error(self) -> None:
        with patch("modulo.db.crud.pipeline.create_pipeline", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await ms.create_pipeline(name="p")
        assert result["error"] == "internal_error"


class TestListPipelinesOwnerFields(_AuthContext):
    async def test_owner_ids_serialised_in_summary(self) -> None:
        item = MagicMock()
        item.id = _PIPELINE_ID
        item.name = "p"
        item.visibility = "org"
        item.business_owner_id = _OWNER_ID
        item.reliability_owner_id = None
        page = MagicMock(items=[item], total=1, next_cursor=None, has_more=False)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.pipeline.list_pipelines", new=AsyncMock(return_value=page)),
        ):
            result = await ms.list_pipelines_tool()
        assert result["data"][0]["business_owner_id"] == str(_OWNER_ID)
        assert result["data"][0]["reliability_owner_id"] is None
