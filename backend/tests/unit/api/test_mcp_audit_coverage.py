"""Unit tests for the MCP audit-coverage decorator (FAR-1472, sweep 7).

The headline test is ``TestDeletePipelineAuditEvent``: it drives a REAL MCP
tool (``delete_pipeline``) end to end and asserts the coarse audit event the
``@mcp_audited(...)`` annotation produces — it fails the moment that annotation
is removed. The rest covers the decorator's failure policy (fail-open vs
fail-closed), the never-mask-the-tool's-own-error rule, and the
never-fabricate-an-actor rules.

Unit tier: no DB — the fresh audit session, the RLS context and the append are
all stubbed; only the wiring between the tool call and the audit event is real.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.api.mcp_audit as mcp_audit
import modulo.api.mcp_server as ms
from modulo.api.mcp_audit import mcp_audited
from tests.unit.api.test_mcp_server_coverage_gaps import _AuthContext

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


@mcp_audited("widget_created", "widget")
async def _fail_open_tool() -> dict[str, Any]:
    return {"id": "w1"}


@mcp_audited("widget_created", "widget", fail_closed=True)
async def _fail_closed_tool() -> dict[str, Any]:
    return {"id": "w1"}


@mcp_audited("widget_deleted", "widget", fail_closed=True)
async def _raising_tool() -> dict[str, Any]:
    raise ValueError("boom")


def _audit_session_mock() -> AsyncMock:
    """Session stand-in supporting the ``async with session.begin()`` block."""
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def audit_env(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    """Isolate the audit write: fresh session, RLS helpers and append stubbed."""
    session = _audit_session_mock()
    append = AsyncMock()

    @asynccontextmanager
    async def _fresh() -> AsyncGenerator[AsyncMock, None]:
        yield session

    monkeypatch.setattr(mcp_audit, "_fresh_session", _fresh)
    monkeypatch.setattr(mcp_audit, "append_audit_event", append)
    monkeypatch.setattr(mcp_audit, "set_rls_org", AsyncMock())
    monkeypatch.setattr(mcp_audit, "set_rls_user_context", AsyncMock())
    return session, append


def _delete_pipeline_patches() -> Any:
    """The tool-side stubs ``delete_pipeline`` needs (auth, scope, CRUD)."""
    return (
        patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
        patch.object(ms, "_pipeline_owner_team_id", new=AsyncMock(return_value=None)),
        patch("modulo.db.crud.pipeline.soft_delete_pipeline", new=AsyncMock(return_value=True)),
    )


class TestDeletePipelineAuditEvent(_AuthContext):
    """A representative mutating MCP tool must emit its audit event."""

    async def test_success_records_a_pipeline_deleted_event(self, audit_env) -> None:
        _session, append = audit_env
        p1, p2, p3 = _delete_pipeline_patches()
        with p1, p2, p3:
            result = await ms.delete_pipeline(pipeline_id=str(_PIPELINE_ID))

        assert result["status"] == "deleted"
        assert append.await_count == 1
        kwargs = append.await_args.kwargs
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["event_type"] == "pipeline_deleted"
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "pipeline"
        assert kwargs["payload_json"] == {"tool": "delete_pipeline", "outcome": "success"}

    async def test_error_outcome_is_recorded_as_error(self, audit_env) -> None:
        _session, append = audit_env
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await ms.delete_pipeline(pipeline_id="not-a-uuid")

        assert result["error"] == "invalid_id"
        assert append.await_count == 1
        assert append.await_args.kwargs["payload_json"]["outcome"] == "error"

    async def test_fail_closed_raises_when_the_chain_refuses_the_event(
        self, audit_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """delete_pipeline is fail-closed: an unauditable deletion is an error."""
        monkeypatch.setattr(mcp_audit, "append_audit_event", AsyncMock(side_effect=RuntimeError("chain down")))
        p1, p2, p3 = _delete_pipeline_patches()
        with p1, p2, p3, pytest.raises(RuntimeError, match="chain down"):
            await ms.delete_pipeline(pipeline_id=str(_PIPELINE_ID))

    def test_annotation_preserves_the_tool_signature(self) -> None:
        """FastMCP derives the tool schema from the decorated callable."""
        assert list(inspect.signature(ms.delete_pipeline).parameters) == ["pipeline_id"]


class TestFailurePolicy(_AuthContext):
    async def test_fail_open_logs_and_returns_the_result(
        self, audit_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(mcp_audit, "append_audit_event", AsyncMock(side_effect=RuntimeError("chain down")))
        with caplog.at_level("WARNING", logger=mcp_audit.__name__):
            result = await _fail_open_tool()

        assert result == {"id": "w1"}
        messages = [record.getMessage() for record in caplog.records]
        assert "mcp_audit.widget_created.append_failed" in messages

    async def test_fail_closed_raises_when_the_append_fails(self, audit_env, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_audit, "append_audit_event", AsyncMock(side_effect=RuntimeError("chain down")))
        with pytest.raises(RuntimeError, match="chain down"):
            await _fail_closed_tool()

    async def test_the_tools_own_error_is_never_replaced_by_the_audit(
        self, audit_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attempt is recorded best-effort, then the original error wins."""
        append = AsyncMock(side_effect=RuntimeError("audit down"))
        monkeypatch.setattr(mcp_audit, "append_audit_event", append)
        with pytest.raises(ValueError, match="boom"):
            await _raising_tool()

        assert append.await_count == 1
        assert append.await_args.kwargs["payload_json"]["outcome"] == "error"

    async def test_missing_org_context_skips_the_event(self, audit_env) -> None:
        """No authenticated tenant: nothing truthful to record, so no write."""
        _session, append = audit_env
        ms._ctx_org_id.set(None)
        try:
            result = await _fail_open_tool()
        finally:
            ms._ctx_org_id.set(_ORG_ID)

        assert result == {"id": "w1"}
        assert append.await_count == 0

    async def test_missing_actor_records_a_null_actor(self, audit_env) -> None:
        """Never fabricate an identity: the event keeps a NULL actor."""
        _session, append = audit_env
        ms._ctx_user_id.set(None)
        try:
            await _fail_open_tool()
        finally:
            ms._ctx_user_id.set(_USER_ID)

        assert append.await_count == 1
        assert append.await_args.kwargs["actor_user_id"] is None
