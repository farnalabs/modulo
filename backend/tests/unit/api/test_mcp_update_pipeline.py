"""FAR-1599: the ``update_pipeline`` MCP tool (environment-profile binding).

Covers the tool end to end: argument validation, the team-scoped-key gate,
the DELEGATION contract (the shared FAR-1558 predicate
``_assert_environment_profile_bindable`` is called — never re-implemented —
with the pipeline's effective owner team), the clear path (no predicate run),
and the error envelopes. Scaffolding mirrors ``test_mcp_pipeline_owners.py``
(``_AuthContext`` sets the MCP ContextVars and neutralises the chokepoint).
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
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

_PREDICATE = "modulo.api.routes.pipelines._assert_environment_profile_bindable"
_CRUD_UPDATE = "modulo.db.crud.pipeline.update_pipeline"


class TestUpdatePipeline(_AuthContext):
    @pytest.fixture(autouse=True)
    def _auth_ok(self):
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            yield

    async def test_auth_failure_returns_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=str(_PROFILE_ID))
        assert result["error"] == "auth_expired"

    async def test_invalid_pipeline_id(self) -> None:
        result = await ms.update_pipeline(pipeline_id="nope", environment_profile_id=None)
        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"

    async def test_unparseable_pipeline_id_without_error_dict(self) -> None:
        """The defensive ``pid is None`` arm: a parser that yields no error
        dict still returns the invalid_id envelope (mirrors the other MCP tool
        shells; ``_parse_uuid_param`` never returns ``(None, None)`` in
        practice, so this branch is only reachable by patching it)."""
        with patch.object(ms, "_parse_uuid_param", return_value=(None, None)):
            result = await ms.update_pipeline(pipeline_id="whatever", environment_profile_id=None)
        assert result == {"error": "invalid_id", "detail": ms._MSG_UUID_PARSE_FAILED}

    async def test_invalid_environment_profile_id(self) -> None:
        result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id="nope")
        assert result["error"] == "invalid_id"
        assert result["field"] == "environment_profile_id"

    async def test_team_boundary_violation(self) -> None:
        """A team-scoped key never reaches the predicate for another team's pipeline."""
        ms._ctx_team_id.set(_TEAM_A)
        try:
            with patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_B)):
                result = await ms.update_pipeline(
                    pipeline_id=str(_PIPELINE_ID), environment_profile_id=str(_PROFILE_ID)
                )
        finally:
            ms._ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"

    async def test_bindable_predicate_is_delegated_with_the_effective_owner_team(self) -> None:
        """The FAR-1558 rule is CALLED, not re-implemented: the shared
        predicate receives the pipeline's owner team, and only a passing
        predicate lets the write reach the CRUD layer."""
        pipeline = MagicMock()
        pipeline.environment_profile_id = _PROFILE_ID
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_A)),
            patch(_PREDICATE, new=AsyncMock(return_value=None)) as predicate,
            patch(_CRUD_UPDATE, new=AsyncMock(return_value=pipeline)) as update,
        ):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=str(_PROFILE_ID))

        assert result == {
            "pipeline_id": str(_PIPELINE_ID),
            "environment_profile_id": str(_PROFILE_ID),
        }
        assert predicate.await_count == 1
        assert predicate.await_args.kwargs["org_id"] == _ORG_ID
        assert predicate.await_args.kwargs["profile_id"] == _PROFILE_ID
        assert predicate.await_args.kwargs["pipeline_owner_team_id"] == _TEAM_A
        assert update.await_args.kwargs["org_id"] == _ORG_ID
        assert update.await_args.kwargs["account_id"] == _USER_ID
        assert update.await_args.args[2] == {"environment_profile_id": _PROFILE_ID}

    async def test_predicate_422_maps_to_validation_failed_with_its_detail(self) -> None:
        """The shared predicate's typed 422 detail (the code clients branch
        on) reaches the caller — never a generic internal error."""
        exc = FastAPIHTTPException(status_code=422, detail="environment_profile_team_mismatch: nope")
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(_PREDICATE, new=AsyncMock(side_effect=exc)),
        ):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=str(_PROFILE_ID))
        assert result["error"] == "validation_failed"
        assert "environment_profile_team_mismatch" in result["detail"]

    async def test_null_clears_without_running_the_predicate(self) -> None:
        """``null`` clears the binding; clearing can never fail on a profile,
        so the existence/team check must NOT run (REST clear-path parity)."""
        pipeline = MagicMock()
        pipeline.environment_profile_id = None
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(_PREDICATE, new=AsyncMock(return_value=None)) as predicate,
            patch(_CRUD_UPDATE, new=AsyncMock(return_value=pipeline)) as update,
        ):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=None)

        assert result == {"pipeline_id": str(_PIPELINE_ID), "environment_profile_id": None}
        assert predicate.await_count == 0
        assert update.await_args.args[2] == {"environment_profile_id": None}

    async def test_pipeline_not_found(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(_CRUD_UPDATE, new=AsyncMock(return_value=None)),
        ):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=None)
        assert result == {"error": "pipeline_not_found", "pipeline_id": str(_PIPELINE_ID)}

    async def test_scope_denial_maps_to_insufficient_scope(self) -> None:
        with patch.object(ms, "_check_agent_tool_scope", side_effect=MCPAuthorizationError("no update_pipeline")):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=None)
        assert result["error"] == "insufficient_scope"

    async def test_programming_error_maps_to_migration_required(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(_CRUD_UPDATE, new=AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception()))),
        ):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=None)
        assert result["error"] == "migration_required"

    async def test_unexpected_error_maps_to_internal_error(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(_CRUD_UPDATE, new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = await ms.update_pipeline(pipeline_id=str(_PIPELINE_ID), environment_profile_id=None)
        assert result["error"] == "server_error"
