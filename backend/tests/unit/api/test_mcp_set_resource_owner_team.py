"""Unit tests for the FAR-2298 ``set_resource_owner_team`` MCP tool.

The tool sets or clears ``owner_team_id`` on pipelines, model backends and
connectors, reusing the REST PATCH team gates. These tests drive the tool end to
end with the CRUD layer patched, so the real gate functions (team existence,
membership, visibility invariant) run against a mock session.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException as FastAPIHTTPException
from sqlalchemy.exc import ProgrammingError

import modulo.api.mcp_server as ms
from modulo.core.mcp.scope_validator import MCPAuthorizationError
from modulo.db.crud.pipeline import PipelineHasActiveRunsError
from tests.unit.api.test_mcp_server_coverage_gaps import _AuthContext, _make_session_context, _mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_RESOURCE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

_MB_GET = "modulo.db.crud.model_backend.get_model_backend"
_MB_UPDATE = "modulo.db.crud.model_backend.update_model_backend"
_CI_GET = "modulo.db.crud.connector_instance.get_connector_instance"
_CI_UPDATE = "modulo.db.crud.connector_instance.update_connector_instance"
_MEMBERSHIP = "modulo.api.team_scope.team_membership_exists"
_REBIND_GUARD = "modulo.api.routes.connectors._reject_re_scope_that_breaks_a_bound_pipeline"
_AUDIT = "modulo.core.audit_logger.append_audit_event"


def _row(*, org_id: uuid.UUID = _ORG_ID, owner: uuid.UUID | None = None, visibility: str = "org") -> SimpleNamespace:
    return SimpleNamespace(organisation_id=org_id, owner_team_id=owner, visibility=visibility)


class _ToolBase(_AuthContext):
    @pytest.fixture(autouse=True)
    def _auth_ok(self):
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            yield

    def _use_session(self, team_exists: bool = True) -> MagicMock:
        """Re-point ``_session`` at a session whose team lookup does (not) find a row."""
        session = _mock_session()
        result = MagicMock()
        result.first.return_value = (_TEAM_A,) if team_exists else None
        session.execute = AsyncMock(return_value=result)
        self._session_patcher.stop()
        self._session_patcher = patch.object(ms, "_session", return_value=_make_session_context(session))
        self._session_patcher.start()
        return session


class TestInputValidation(_ToolBase):
    async def test_auth_failure(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), None)
        assert result["error"] == "auth_expired"

    async def test_unknown_resource_type_is_validation_failed(self) -> None:
        result = await ms.set_resource_owner_team("agent", str(_RESOURCE_ID), None)
        assert result["error"] == "validation_failed"

    async def test_invalid_resource_id(self) -> None:
        result = await ms.set_resource_owner_team("pipeline", "nope", None)
        assert result["error"] == "invalid_id"
        assert result["field"] == "resource_id"

    async def test_invalid_owner_team_id(self) -> None:
        result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), "nope")
        assert result["error"] == "invalid_id"
        assert result["field"] == "owner_team_id"

    async def test_scope_denial_maps_to_insufficient_scope(self) -> None:
        with patch.object(ms, "check_tool_scope", side_effect=MCPAuthorizationError("nope")):
            result = await ms.set_resource_owner_team("connector", str(_RESOURCE_ID), None)
        assert result["error"] == "insufficient_scope"

    async def test_scope_check_uses_resource_type_as_action(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch.object(ms, "_apply_owner_team_pipeline", new=AsyncMock(return_value=(None, None))),
        ):
            await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), None)
        assert ms.check_tool_scope.call_args.args[1] == "set_resource_owner_team"
        assert ms.check_tool_scope.call_args.kwargs["action"] == "pipeline"


class TestPipeline(_ToolBase):
    async def test_assign_returns_previous_and_stored(self) -> None:
        apply = AsyncMock(return_value=(None, _TEAM_A))
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch.object(ms, "_apply_owner_team_pipeline", new=apply),
        ):
            result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), str(_TEAM_A))
        assert result == {
            "resource_type": "pipeline",
            "resource_id": str(_RESOURCE_ID),
            "previous_owner_team_id": None,
            "owner_team_id": str(_TEAM_A),
        }
        assert apply.await_args.args[2] == _RESOURCE_ID
        assert apply.await_args.args[3] == _TEAM_A

    async def test_clear_passes_none(self) -> None:
        apply = AsyncMock(return_value=(_TEAM_A, None))
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_A)),
            patch.object(ms, "_apply_owner_team_pipeline", new=apply),
        ):
            result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), None)
        assert result["owner_team_id"] is None
        assert result["previous_owner_team_id"] == str(_TEAM_A)
        assert apply.await_args.args[3] is None

    async def test_unknown_pipeline(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch.object(ms, "_apply_owner_team_pipeline", new=AsyncMock(return_value=None)),
        ):
            result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), None)
        assert result == {"error": "pipeline_not_found", "resource_id": str(_RESOURCE_ID)}

    async def test_team_scoped_key_cannot_touch_other_team(self) -> None:
        ms._ctx_team_id.set(_TEAM_A)
        try:
            with patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=_TEAM_B)):
                result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), None)
        finally:
            ms._ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"

    async def test_active_runs_block_the_move(self) -> None:
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch.object(ms, "_apply_owner_team_pipeline", new=AsyncMock(side_effect=PipelineHasActiveRunsError(3))),
        ):
            result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), str(_TEAM_A))
        assert result["error"] == "pipeline_has_active_runs"

    async def test_gate_rejection_is_validation_failed(self) -> None:
        exc = FastAPIHTTPException(
            status_code=403, detail="Cannot reassign a pipeline to a team you are not a member of"
        )
        with (
            patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch.object(ms, "_apply_owner_team_pipeline", new=AsyncMock(side_effect=exc)),
        ):
            result = await ms.set_resource_owner_team("pipeline", str(_RESOURCE_ID), str(_TEAM_A))
        assert result["error"] == "validation_failed"
        assert "not a member" in result["detail"]

    async def test_helper_runs_rest_gates_then_updates(self) -> None:
        locked = _row(owner=None)
        principal = SimpleNamespace(organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
        updated = _row(owner=_TEAM_A)
        update = AsyncMock(return_value=updated)
        gate = AsyncMock(return_value=locked)
        transition = AsyncMock()
        bindings = AsyncMock()
        team_check = AsyncMock()
        with (
            patch("modulo.api.routes.pipelines._reapply_team_gate_inside_mutation_txn", new=gate),
            patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=transition),
            patch("modulo.api.routes.pipelines._enforce_stored_bindings_on_team_change", new=bindings),
            patch("modulo.api.team_scope.validate_owner_team_for_create", new=team_check),
            patch("modulo.db.crud.pipeline.update_pipeline", new=update),
        ):
            outcome = await ms._apply_owner_team_pipeline(MagicMock(), principal, _RESOURCE_ID, _TEAM_A, _ORG_ID)
        assert outcome == (None, _TEAM_A)
        assert transition.await_args.args[3] == {"owner_team_id": _TEAM_A}
        assert bindings.await_args.kwargs["ownership_changed"] is True
        assert team_check.await_args.args[2] == _TEAM_A
        assert update.await_args.args[2] == {"owner_team_id": _TEAM_A}
        assert update.await_args.kwargs == {"org_id": _ORG_ID, "account_id": _USER_ID}

    async def test_helper_gone_pipeline_returns_none(self) -> None:
        principal = SimpleNamespace(organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
        gone = FastAPIHTTPException(status_code=404, detail="Pipeline not found")
        with patch(
            "modulo.api.routes.pipelines._reapply_team_gate_inside_mutation_txn", new=AsyncMock(side_effect=gone)
        ):
            outcome = await ms._apply_owner_team_pipeline(MagicMock(), principal, _RESOURCE_ID, None, _ORG_ID)
        assert outcome is None

    async def test_helper_reraises_non_404_gate_error(self) -> None:
        principal = SimpleNamespace(organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
        boom = FastAPIHTTPException(status_code=403, detail="denied")
        with (
            patch(
                "modulo.api.routes.pipelines._reapply_team_gate_inside_mutation_txn", new=AsyncMock(side_effect=boom)
            ),
            pytest.raises(FastAPIHTTPException),
        ):
            await ms._apply_owner_team_pipeline(MagicMock(), principal, _RESOURCE_ID, None, _ORG_ID)

    async def test_helper_clear_skips_team_validation(self) -> None:
        principal = SimpleNamespace(organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
        locked = _row(owner=_TEAM_A)
        update = AsyncMock(return_value=_row(owner=None))
        team_check = AsyncMock()
        with (
            patch(
                "modulo.api.routes.pipelines._reapply_team_gate_inside_mutation_txn",
                new=AsyncMock(return_value=locked),
            ),
            patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
            patch("modulo.api.routes.pipelines._enforce_stored_bindings_on_team_change", new=AsyncMock()),
            patch("modulo.api.team_scope.validate_owner_team_for_create", new=team_check),
            patch("modulo.db.crud.pipeline.update_pipeline", new=update),
        ):
            outcome = await ms._apply_owner_team_pipeline(MagicMock(), principal, _RESOURCE_ID, None, _ORG_ID)
        assert outcome == (_TEAM_A, None)
        team_check.assert_not_awaited()

    async def test_helper_update_returns_none_after_gate(self) -> None:
        principal = SimpleNamespace(organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
        with (
            patch(
                "modulo.api.routes.pipelines._reapply_team_gate_inside_mutation_txn",
                new=AsyncMock(return_value=_row(owner=None)),
            ),
            patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
            patch("modulo.api.routes.pipelines._enforce_stored_bindings_on_team_change", new=AsyncMock()),
            patch("modulo.api.team_scope.validate_owner_team_for_create", new=AsyncMock()),
            patch("modulo.db.crud.pipeline.update_pipeline", new=AsyncMock(return_value=None)),
        ):
            outcome = await ms._apply_owner_team_pipeline(MagicMock(), principal, _RESOURCE_ID, _TEAM_A, _ORG_ID)
        assert outcome is None


@pytest.mark.parametrize(
    ("resource_type", "get_target", "update_target"),
    [("model_backend", _MB_GET, _MB_UPDATE), ("connector", _CI_GET, _CI_UPDATE)],
)
class TestSimpleResources(_ToolBase):
    async def test_admin_assigns_team(self, resource_type: str, get_target: str, update_target: str) -> None:
        ms._ctx_role.set("admin")
        self._use_session()
        row = _row(owner=None)
        update = AsyncMock(return_value=_row(owner=_TEAM_A))
        with (
            patch(get_target, new=AsyncMock(return_value=row)),
            patch(update_target, new=update),
            patch(_REBIND_GUARD, new=AsyncMock()),
            patch(_AUDIT, new=AsyncMock()) as audit,
        ):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), str(_TEAM_A))
        assert result == {
            "resource_type": resource_type,
            "resource_id": str(_RESOURCE_ID),
            "previous_owner_team_id": None,
            "owner_team_id": str(_TEAM_A),
        }
        assert update.await_args.args[2] == {"owner_team_id": _TEAM_A}
        audit_kwargs = audit.await_args.kwargs
        assert audit_kwargs["event_type"] == "resource_team_ownership_changed"
        assert audit_kwargs["payload_json"]["new_team_id"] == str(_TEAM_A)

    async def test_member_clears_org_visible_resource(
        self, resource_type: str, get_target: str, update_target: str
    ) -> None:
        self._use_session()
        row = _row(owner=_TEAM_A, visibility="org")
        update = AsyncMock(return_value=_row(owner=None))
        with (
            patch(get_target, new=AsyncMock(return_value=row)),
            patch(update_target, new=update),
            patch(_REBIND_GUARD, new=AsyncMock()),
            patch(_AUDIT, new=AsyncMock()),
        ):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), None)
        assert result["owner_team_id"] is None
        assert result["previous_owner_team_id"] == str(_TEAM_A)
        assert update.await_args.args[2] == {"owner_team_id": None}

    async def test_clear_on_team_visible_resource_is_rejected(
        self, resource_type: str, get_target: str, update_target: str
    ) -> None:
        ms._ctx_role.set("admin")
        self._use_session()
        update = AsyncMock()
        with (
            patch(get_target, new=AsyncMock(return_value=_row(owner=_TEAM_A, visibility="team"))),
            patch(update_target, new=update),
        ):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), None)
        assert result["error"] == "validation_failed"
        assert "owner_team_id is required" in result["detail"]
        update.assert_not_awaited()

    async def test_unknown_team_is_validation_failed_even_for_admin(
        self, resource_type: str, get_target: str, update_target: str
    ) -> None:
        ms._ctx_role.set("admin")
        self._use_session(team_exists=False)
        update = AsyncMock()
        with patch(get_target, new=AsyncMock(return_value=_row())), patch(update_target, new=update):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), str(_TEAM_B))
        assert result["error"] == "validation_failed"
        assert "not found in this organisation" in result["detail"]
        update.assert_not_awaited()

    async def test_non_member_cannot_assign_team(self, resource_type: str, get_target: str, update_target: str) -> None:
        self._use_session()
        update = AsyncMock()
        with (
            patch(get_target, new=AsyncMock(return_value=_row())),
            patch(update_target, new=update),
            patch(_MEMBERSHIP, new=AsyncMock(return_value=False)),
        ):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), str(_TEAM_A))
        assert result["error"] == "validation_failed"
        assert "not a member" in result["detail"]
        update.assert_not_awaited()

    async def test_unknown_resource(self, resource_type: str, get_target: str, update_target: str) -> None:
        with patch(get_target, new=AsyncMock(return_value=None)):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), None)
        assert result == {"error": f"{resource_type}_not_found", "resource_id": str(_RESOURCE_ID)}

    async def test_resource_in_other_org_is_not_found(
        self, resource_type: str, get_target: str, update_target: str
    ) -> None:
        update = AsyncMock()
        with (
            patch(get_target, new=AsyncMock(return_value=_row(org_id=_OTHER_ORG_ID))),
            patch(update_target, new=update),
        ):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), None)
        assert result["error"] == f"{resource_type}_not_found"
        update.assert_not_awaited()

    async def test_team_scoped_key_cannot_touch_other_team(
        self, resource_type: str, get_target: str, update_target: str
    ) -> None:
        ms._ctx_role.set("admin")
        self._use_session()
        ms._ctx_team_id.set(_TEAM_A)
        try:
            with (
                patch(get_target, new=AsyncMock(return_value=_row(owner=_TEAM_B))),
                patch(update_target, new=AsyncMock(return_value=_row(owner=None))),
                patch(_REBIND_GUARD, new=AsyncMock()),
                patch(_AUDIT, new=AsyncMock()),
            ):
                result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), None)
        finally:
            ms._ctx_team_id.set(None)
        assert result["error"] == "team_boundary_violation"

    async def test_helper_update_returns_none(self, resource_type: str, get_target: str, update_target: str) -> None:
        principal = SimpleNamespace(organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
        with (
            patch(get_target, new=AsyncMock(return_value=_row(owner=_TEAM_A))),
            patch(update_target, new=AsyncMock(return_value=None)),
            patch(_REBIND_GUARD, new=AsyncMock()),
        ):
            outcome = await ms._apply_owner_team_simple(
                _mock_session(), principal, resource_type, _RESOURCE_ID, None, _ORG_ID
            )
        assert outcome is None

    async def test_same_team_change_writes_no_audit(
        self, resource_type: str, get_target: str, update_target: str
    ) -> None:
        ms._ctx_role.set("admin")
        self._use_session()
        with (
            patch(get_target, new=AsyncMock(return_value=_row(owner=_TEAM_A))),
            patch(update_target, new=AsyncMock(return_value=_row(owner=_TEAM_A))),
            patch(_REBIND_GUARD, new=AsyncMock()),
            patch(_AUDIT, new=AsyncMock()) as audit,
        ):
            result = await ms.set_resource_owner_team(resource_type, str(_RESOURCE_ID), str(_TEAM_A))
        assert result["owner_team_id"] == str(_TEAM_A)
        audit.assert_not_awaited()


class TestConnectorRebindGuard(_ToolBase):
    async def test_strand_guard_rejection_is_validation_failed(self) -> None:
        ms._ctx_role.set("admin")
        self._use_session()
        guard = AsyncMock(side_effect=FastAPIHTTPException(status_code=409, detail="connector_team_mismatch: x"))
        update = AsyncMock()
        with (
            patch(_CI_GET, new=AsyncMock(return_value=_row())),
            patch(_CI_UPDATE, new=update),
            patch(_REBIND_GUARD, new=guard),
        ):
            result = await ms.set_resource_owner_team("connector", str(_RESOURCE_ID), str(_TEAM_A))
        assert result["error"] == "validation_failed"
        assert "connector_team_mismatch" in result["detail"]
        update.assert_not_awaited()


class TestUnexpectedErrors(_ToolBase):
    async def test_programming_error_maps_to_migration_required(self) -> None:
        with patch(_MB_GET, new=AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("boom")))):
            result = await ms.set_resource_owner_team("model_backend", str(_RESOURCE_ID), None)
        assert result["error"] == "migration_required"

    async def test_unexpected_exception_maps_to_server_error(self) -> None:
        with patch(_MB_GET, new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await ms.set_resource_owner_team("model_backend", str(_RESOURCE_ID), None)
        assert result["error"] == "server_error"
