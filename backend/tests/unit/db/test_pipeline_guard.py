"""Service-layer backstop guard tests for replace_pipeline_graph /
rollback_to_snapshot (hitl-gate-removal-guard-plan.md v19 §3, §7)."""

from __future__ import annotations

import copy
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.auth.permissions import reset_authz_enforce, set_authz_enforce
from modulo.db.crud.hitl_gate_guard import (
    REASON_CORRELATION_KEY_MISMATCH,
    REASON_INSUFFICIENT_ROLE,
    REASON_LEGACY_SNAPSHOT_AMBIGUOUS,
    REASON_MCP_NOT_PERMITTED,
    REASON_ROLE_CHANGED,
    REASON_ROLE_CHECK_DB_ERROR,
    DiffResult,
    HitlGateWeakeningDenied,
)
from modulo.db.crud.pipeline import _edge_to_plain_dict, replace_pipeline_graph
from modulo.db.crud.pipeline_snapshot_versioning import rollback_to_snapshot

pytestmark = pytest.mark.asyncio(loop_scope="module")

_NODE_A = "00000000-0000-0000-0000-0000000000a1"
_NODE_B = "00000000-0000-0000-0000-0000000000a2"

_GATE = {
    "human_only": True,
    "required_team_id": None,
    "condition": None,
    "eval_condition": None,
    "claim_expiry_minutes": 60,
}


class _PipelineRow:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = uuid.uuid4()
        self.deleted_at = None
        self.graph_nodes_json = []


class _EdgeRow:
    def __init__(
        self,
        source: str = _NODE_A,
        target: str = _NODE_B,
        edge_type: str = "normal",
        cfg: dict | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.source_node_id = uuid.UUID(source)
        self.target_node_id = uuid.UUID(target)
        self.edge_type = edge_type
        self.hitl_gate_config = copy.deepcopy(cfg)
        self.condition_expression = None
        self.source_port = None
        self.target_port = None


class _SnapshotRow:
    def __init__(self, graph_json: dict, pipeline_id: uuid.UUID | None = None) -> None:
        self.id = uuid.uuid4()
        self.pipeline_id = pipeline_id or uuid.uuid4()
        self.graph_json = graph_json
        self.snapshot_version = 1


def _build_session(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    calls = list(results)

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        if calls:
            return calls.pop(0)
        # Default result for statements we don't need to control (e.g. the
        # delete(PipelineEdge) that runs after the guard passes, and the
        # guardrail-rows query the service-layer strip guard runs). The
        # guardrail-rows SELECT returns no bound guardrails by default.
        if _is_guardrail_rows_query(stmt):
            return _empty_scalars_result()
        return MagicMock()

    session.execute = AsyncMock(side_effect=_execute)
    # add/add_all are synchronous on AsyncSession; keep them sync mocks so the
    # write path doesn't discard unawaited coroutines.
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    return session


def _is_guardrail_rows_query(stmt: object) -> bool:
    return "FROM eval_definitions" in str(stmt)


def _pipeline_result(pipeline: _PipelineRow) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = pipeline
    return result


def _edges_result(edges: list[_EdgeRow]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = list(edges)
    return result


def _empty_scalars_result() -> MagicMock:
    """A result whose ``scalars().all()`` is an empty list (guardrail rows)."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def _snapshot_result(snapshot: _SnapshotRow | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = snapshot
    return result


def _weakening_edges(old_cfg: dict, new_cfg: dict) -> tuple[list[_EdgeRow], list[dict]]:
    old = [_EdgeRow(cfg=old_cfg)]
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": new_cfg,
            "hitl_gate_config_present": True,
        }
    ]
    return old, new


# ---------------------------------------------------------------------------
# Guard runs before any delete/insert
# ---------------------------------------------------------------------------


async def test_replace_pipeline_graph_guard_runs_before_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=False,
            caller_type="rest",
        )
    assert excinfo.value.reason_code == REASON_INSUFFICIENT_ROLE
    # The deny happened before the delete/insert executes: execute was called
    # for the row lock + the edge load + the guardrail-rows query (no delete
    # statement).
    assert session.execute.await_count == 3
    session.add_all.assert_not_called()
    audit.assert_not_awaited()  # denied path: no allowed-weakening audit


async def test_rollback_to_snapshot_guard_runs_before_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    snapshot = _SnapshotRow({"nodes": [], "edges": []}, pipeline_id=pipeline.id)  # drops the gated edge
    old = [_EdgeRow(cfg=dict(_GATE))]
    session = _build_session(_snapshot_result(snapshot), _pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline_snapshot_versioning.append_audit_event", audit)
    monkeypatch.setattr(
        "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
        AsyncMock(return_value=_SnapshotRow({"nodes": [], "edges": []})),
    )

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await rollback_to_snapshot(
            session,
            pipeline.id,
            snapshot.id,
            is_privileged=False,
            caller_type="rest",
        )
    assert excinfo.value.reason_code == REASON_LEGACY_SNAPSHOT_AMBIGUOUS
    assert session.execute.await_count == 4  # snapshot + row lock + edge load + guardrail rows
    audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Live-role check under the lock + fail-closed no-retry
# ---------------------------------------------------------------------------


async def test_live_role_check_runs_after_the_row_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    events: list[str] = []

    async def on_lock() -> None:
        events.append("lock_acquired")

    async def fake_role(session_obj: object, account_id: str, organisation_id: str) -> str:
        assert "lock_acquired" in events, "role query must run only after the row lock is held"
        events.append("role_query")
        return "admin"

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", fake_role)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=False,
        caller_type="rest",
        account_id=uuid.uuid4(),
        _on_lock_acquired=on_lock,
    )
    assert result is not None
    # Both the HITL privilege check AND the guardrail-admin check re-read the
    # live role under the lock (FAR-309 PR A review).
    assert events == ["lock_acquired", "role_query", "role_query"]


async def test_fail_closed_no_retry_on_role_query_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))

    calls = 0

    async def fake_role(session_obj: object, account_id: str, organisation_id: str) -> str:
        nonlocal calls
        calls += 1
        raise SQLAlchemyError("boom")

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", fake_role)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=True,
            caller_type="rest",
            account_id=uuid.uuid4(),
        )
    assert excinfo.value.reason_code == REASON_ROLE_CHECK_DB_ERROR
    assert calls == 1, "no second role query may be attempted"


async def test_missing_membership_denies_with_role_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))

    async def fake_role(session_obj: object, account_id: str, organisation_id: str) -> None:
        return None

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", fake_role)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=True,
            caller_type="rest",
            account_id=uuid.uuid4(),
        )
    assert excinfo.value.reason_code == REASON_ROLE_CHANGED


# ---------------------------------------------------------------------------
# Non-liftable: the guard never consults the authz kill switch
# ---------------------------------------------------------------------------


async def test_non_liftable_regardless_of_authz_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the general kill switch OFF (authz_enforce=false), a non-admin
    weakening attempt is still denied — the guard never reads the flag."""
    token = set_authz_enforce(False)
    try:
        pipeline = _PipelineRow()
        old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
        session = _build_session(_pipeline_result(pipeline), _edges_result(old))

        # If the guard ever consulted the kill switch it would call
        # resolve_authz_enforce — make that a loud failure.
        def _fail_switch(*_a: object, **_k: object) -> None:
            raise AssertionError("guard must not consult the authz kill switch")

        monkeypatch.setattr("modulo.db.settings_resolver.resolve_authz_enforce", _fail_switch)

        with pytest.raises(HitlGateWeakeningDenied) as excinfo:
            await replace_pipeline_graph(
                session,
                pipeline_id=pipeline.id,
                org_id=uuid.uuid4(),
                nodes=[],
                edges=new,
                is_privileged=False,
                caller_type="rest",
            )
        assert excinfo.value.reason_code == REASON_INSUFFICIENT_ROLE
    finally:
        reset_authz_enforce(token)


# ---------------------------------------------------------------------------
# MCP structural exclusion at the guarded function level
# ---------------------------------------------------------------------------


async def test_mcp_caller_type_is_always_denied_even_when_privileged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=True,  # would be privileged over REST
            caller_type="mcp",
        )
    assert excinfo.value.reason_code == REASON_MCP_NOT_PERMITTED
    audit.assert_not_awaited()


async def test_mcp_non_weakening_write_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP can still perform non-weakening graph writes (gate untouched)."""
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=None)]  # no gate on the old edge
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": None,
            "hitl_gate_config_present": True,
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=True,
        caller_type="mcp",
    )
    assert result is not None
    audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Denied-then-allowed (admin)
# ---------------------------------------------------------------------------


async def test_denied_then_allowed_for_privileged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-privileged weakening is denied; the same write with privilege is
    allowed and emits the hitl_gate_removed audit event."""
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    audit = AsyncMock()

    # Denied for non-privileged.
    pipeline = _PipelineRow()
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)
    with pytest.raises(HitlGateWeakeningDenied):
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=False,
            caller_type="rest",
        )
    audit.assert_not_awaited()

    # Allowed for privileged (admin) — the write proceeds and audit fires.
    async def fake_role(session_obj: object, account_id: str, organisation_id: str) -> str:
        return "admin"

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", fake_role)

    pipeline = _PipelineRow()
    session2 = _build_session(_pipeline_result(pipeline), _edges_result(old))
    result = await replace_pipeline_graph(
        session2,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=True,
        caller_type="rest",
        account_id=uuid.uuid4(),
    )
    assert result is not None
    audit.assert_awaited_once()
    call_kwargs = audit.call_args.kwargs
    assert call_kwargs["event_type"] == "hitl_gate_removed"
    assert call_kwargs["payload_json"]["caller_type"] == "rest"
    assert call_kwargs["payload_json"]["denied"] is False
    assert call_kwargs["payload_json"]["affected_edges"][0]["weakening_types"] == ["human_only"]


async def test_allowed_weakening_with_live_admin_role_under_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """caller_type='rest' + live admin role: the live role is authoritative."""
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    async def fake_role(session_obj: object, account_id: str, organisation_id: str) -> str:
        return "admin"

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", fake_role)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=False,  # stale flag; the live role overrides to privileged
        caller_type="rest",
        account_id=uuid.uuid4(),
    )
    assert result is not None
    audit.assert_awaited_once()


async def test_live_role_demotion_denies_even_when_route_flag_privileged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live role lower than the route-time flag is fail-closed (deny)."""
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))

    async def fake_role(session_obj: object, account_id: str, organisation_id: str) -> str:
        return "viewer"

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", fake_role)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=True,  # route-time flag said privileged
            caller_type="rest",
            account_id=uuid.uuid4(),
        )
    assert excinfo.value.reason_code == REASON_INSUFFICIENT_ROLE


# ---------------------------------------------------------------------------
# Concurrent-graph-replace race: _on_lock_acquired is a no-op by default
# ---------------------------------------------------------------------------


async def test_on_lock_acquired_is_noop_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    old, new = _weakening_edges(dict(_GATE), {**_GATE, "human_only": False})
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=True,
        caller_type="rest",
    )
    assert result is not None


async def test_on_lock_acquired_hook_invoked_after_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    session = _build_session(_pipeline_result(pipeline), _edges_result([]))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    events: list[str] = []

    async def on_lock() -> None:
        events.append("lock")

    await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=[],
        is_privileged=True,
        caller_type="rest",
        _on_lock_acquired=on_lock,
    )
    assert events == ["lock"]


# ---------------------------------------------------------------------------
# Structural (deletion) denial reason
# ---------------------------------------------------------------------------


async def test_edge_deletion_denied_with_correlation_key_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=dict(_GATE))]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=[],  # drops the gated edge entirely
            is_privileged=False,
            caller_type="rest",
        )
    assert excinfo.value.reason_code == REASON_CORRELATION_KEY_MISMATCH
    assert excinfo.value.payload_json["affected_edges"][0]["weakening_types"] == ["structural:edge_deleted"]


# ---------------------------------------------------------------------------
# Preserve-write: an omitted hitl_gate_config key must NOT wipe the stored gate
# ---------------------------------------------------------------------------


async def test_replace_preserves_gate_when_config_key_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing new edges that OMIT the hitl_gate_config key on an existing gated
    edge preserves the stored gate (guard contract: omission = preserve). No
    weakening, no audit, no denial."""
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=dict(_GATE))]
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            # hitl_gate_config key deliberately omitted (untouched edge)
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=False,
        caller_type="rest",
    )
    assert result is not None
    persisted = result[1]
    assert len(persisted) == 1
    assert persisted[0].hitl_gate_config == _GATE, "omitted key must preserve the stored gate, not write None"
    audit.assert_not_awaited()


async def test_replace_preserves_gate_when_presence_flag_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """hitl_gate_config_present=False on a matching topology key also preserves
    the stored gate even when the dict carries an explicit null value."""
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=dict(_GATE))]
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": None,
            "hitl_gate_config_present": False,
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=False,
        caller_type="rest",
    )
    assert result is not None
    persisted = result[1]
    assert persisted[0].hitl_gate_config == _GATE
    audit.assert_not_awaited()


async def test_replace_omitted_key_on_new_edge_still_writes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brand-new edge (no prior stored gate) with an omitted key persists
    None — preservation only applies to matching gated edges."""
    pipeline = _PipelineRow()
    old = []  # no prior edges at all
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=False,
        caller_type="rest",
    )
    assert result is not None
    persisted = result[1]
    assert persisted[0].hitl_gate_config is None
    audit.assert_not_awaited()


async def test_replace_explicit_null_still_denies_for_non_privileged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EXPLICIT hitl_gate_config: None (present) on a gated edge is a genuine
    removal — non-privileged callers are still denied."""
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=dict(_GATE))]
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": None,
            "hitl_gate_config_present": True,
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=False,
            caller_type="rest",
        )
    assert excinfo.value.reason_code == REASON_CORRELATION_KEY_MISMATCH
    audit.assert_not_awaited()


async def test_replace_explicit_null_writes_none_for_privileged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EXPLICIT hitl_gate_config: None is a genuine removal — a privileged
    caller's write persists None (not the old gate) and the allowed-weakening
    audit fires."""
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=dict(_GATE))]
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": None,
            "hitl_gate_config_present": True,
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=True,
        caller_type="rest",
    )
    assert result is not None
    persisted = result[1]
    assert persisted[0].hitl_gate_config is None
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["event_type"] == "hitl_gate_removed"
    assert audit.call_args.kwargs["payload_json"]["affected_edges"][0]["weakening_types"] == ["structural:gate_removed"]


async def test_replace_explicit_null_still_denies_for_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """An MCP caller cannot strip a gate via an explicit null either — the
    structural exclusion forces denial even though the caller is 'privileged'."""
    pipeline = _PipelineRow()
    old = [_EdgeRow(cfg=dict(_GATE))]
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": None,
            "hitl_gate_config_present": True,
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    with pytest.raises(HitlGateWeakeningDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=[],
            edges=new,
            is_privileged=True,
            caller_type="mcp",
        )
    assert excinfo.value.reason_code == REASON_MCP_NOT_PERMITTED
    audit.assert_not_awaited()


async def test_replace_present_false_with_config_on_new_edge_ignores_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contradictory input (hitl_gate_config_present=False alongside a non-null
    hitl_gate_config) on a brand-new edge is ignored by the write path, matching
    the guard which treats present=False as omission. The provided value must
    NOT be persisted via the no-old-value fallback."""
    pipeline = _PipelineRow()
    old = []  # brand-new edge: no prior gate to preserve
    new = [
        {
            "id": uuid.uuid4(),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": dict(_GATE),
            "hitl_gate_config_present": False,
        }
    ]
    session = _build_session(_pipeline_result(pipeline), _edges_result(old))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=new,
        is_privileged=False,
        caller_type="rest",
    )
    assert result is not None
    persisted = result[1]
    assert persisted[0].hitl_gate_config is None, "present=False must ignore the provided gate value"
    audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-309 PR A review � service-layer guardrail-binding strip guard
# ---------------------------------------------------------------------------


def _guardrail_row(node_id: str) -> _EdgeRow:
    row = MagicMock()
    row.node_id = uuid.UUID(node_id)
    return row


def _guardrail_rows_result(rows: list[MagicMock]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


async def test_replace_pipeline_graph_denies_guardrail_binding_strip_for_nonadmin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NON-ADMIN replacing the graph and removing a guardrail-bound node is
    denied (FAR-309 PR A review service-layer guard). The strip guard runs
    under the row lock, before any delete/insert."""
    from modulo.db.crud.hitl_gate_guard import GuardrailBindingStripDenied

    pipeline = _PipelineRow()
    bound_node = "00000000-0000-0000-0000-0000000000c1"
    nodes = [{"id": _NODE_A, "agent_id": "ag1"}]
    session = _build_session(
        _pipeline_result(pipeline),
        _edges_result([]),
        _guardrail_rows_result([_guardrail_row(bound_node)]),
    )
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    with pytest.raises(GuardrailBindingStripDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=nodes,
            edges=[],
            is_privileged=False,
            caller_type="rest",
        )
    assert str(bound_node) in excinfo.value.stripped_node_ids
    assert "strip a guardrail binding" in excinfo.value.detail
    session.add_all.assert_not_called()


async def test_replace_pipeline_graph_allows_guardrail_strip_for_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ADMIN may remove a guardrail-bound node from the graph (admin owns
    guardrail management via ``guardrail.manage``)."""
    pipeline = _PipelineRow()
    bound_node = "00000000-0000-0000-0000-0000000000c1"
    nodes = [{"id": _NODE_A, "agent_id": "ag1"}]
    session = _build_session(
        _pipeline_result(pipeline),
        _edges_result([]),
        _guardrail_rows_result([_guardrail_row(bound_node)]),
    )
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)
    monkeypatch.setattr(
        "modulo.db.crud.pipeline.apply_gated_edge_diff",
        AsyncMock(
            return_value=DiffResult(
                weakened_edges=[],
                has_weakening=False,
                denied=False,
                reason_code=None,
                caller_type="rest",
            )
        ),
    )

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=nodes,
        edges=[],
        is_privileged=True,
        caller_type="rest",
        is_guardrail_admin=True,
    )
    assert result is not None


async def test_replace_pipeline_graph_denies_guardrail_strip_via_live_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service-layer guard re-reads the caller's live role under the lock:
    a caller whose route flag claims admin but whose live membership is NOT
    admin is denied (fail-closed on a stale admin claim)."""
    from modulo.db.crud.hitl_gate_guard import GuardrailBindingStripDenied

    pipeline = _PipelineRow()
    bound_node = "00000000-0000-0000-0000-0000000000c1"
    nodes = [{"id": _NODE_A, "agent_id": "ag1"}]
    session = _build_session(
        _pipeline_result(pipeline),
        _edges_result([]),
        _guardrail_rows_result([_guardrail_row(bound_node)]),
    )
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    async def _fake_role(*_a: object, **_k: object) -> str:
        return "operator"

    monkeypatch.setattr("modulo.db.crud.org_membership.resolve_role_from_membership", _fake_role)

    with pytest.raises(GuardrailBindingStripDenied) as excinfo:
        await replace_pipeline_graph(
            session,
            pipeline_id=pipeline.id,
            org_id=uuid.uuid4(),
            nodes=nodes,
            edges=[],
            is_privileged=True,
            caller_type="rest",
            account_id=uuid.uuid4(),
            is_guardrail_admin=True,
        )
    assert "strip a guardrail binding" in excinfo.value.detail


async def test_rollback_to_snapshot_denies_guardrail_binding_strip_for_nonadmin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NON-ADMIN rolling back to a snapshot whose graph LACKS a currently
    guardrail-bound node is denied (FAR-309 PR A review service-layer guard)."""
    from modulo.db.crud.hitl_gate_guard import GuardrailBindingStripDenied

    pipeline = _PipelineRow()
    bound_node = "00000000-0000-0000-0000-0000000000c1"
    snapshot = _SnapshotRow(
        {"nodes": [{"id": _NODE_A, "agent_id": "ag1"}], "edges": []},
        pipeline_id=pipeline.id,
    )
    session = _build_session(
        _snapshot_result(snapshot),
        _pipeline_result(pipeline),
        _edges_result([]),
        _guardrail_rows_result([_guardrail_row(bound_node)]),
    )
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline_snapshot_versioning.append_audit_event", audit)

    with pytest.raises(GuardrailBindingStripDenied) as excinfo:
        await rollback_to_snapshot(
            session,
            pipeline.id,
            snapshot.id,
            is_privileged=False,
            caller_type="rest",
        )
    assert str(bound_node) in excinfo.value.stripped_node_ids
    session.add.assert_not_called()


async def test_rollback_to_snapshot_allows_guardrail_strip_for_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ADMIN may roll back to a snapshot that drops a guardrail-bound node."""
    pipeline = _PipelineRow()
    bound_node = "00000000-0000-0000-0000-0000000000c1"
    snapshot = _SnapshotRow(
        {"nodes": [{"id": _NODE_A, "agent_id": "ag1"}], "edges": []},
        pipeline_id=pipeline.id,
    )
    session = _build_session(
        _snapshot_result(snapshot),
        _pipeline_result(pipeline),
        _edges_result([]),
        _guardrail_rows_result([_guardrail_row(bound_node)]),
    )
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline_snapshot_versioning.append_audit_event", audit)
    monkeypatch.setattr(
        "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
        AsyncMock(
            return_value=DiffResult(
                weakened_edges=[],
                has_weakening=False,
                denied=False,
                reason_code=None,
                caller_type="rest",
            )
        ),
    )
    monkeypatch.setattr(
        "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
        AsyncMock(return_value=_SnapshotRow({"nodes": [], "edges": []})),
    )

    result = await rollback_to_snapshot(
        session,
        pipeline.id,
        snapshot.id,
        is_privileged=True,
        caller_type="rest",
        is_guardrail_admin=True,
    )
    assert result is not None


# ---------------------------------------------------------------------------
# FAR-455: condition_expression is persisted on conditional edges
# ---------------------------------------------------------------------------


async def test_replace_pipeline_graph_persists_condition_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conditional edge whose dict carries ``condition_expression`` must have
    that expression persisted onto the stored PipelineEdge row, not silently
    dropped (was GraphValidationError CONDITION_MISSING_EXPRESSION at run time).
    """
    expr = "result.answer != 'UNKNOWN'"
    edge = {
        "id": uuid.uuid4(),
        "source_node_id": _NODE_A,
        "target_node_id": _NODE_B,
        "edge_type": "conditional",
        "condition_expression": expr,
        "hitl_gate_config": None,
        "hitl_gate_config_present": True,
    }
    pipeline = _PipelineRow()
    session = _build_session(_pipeline_result(pipeline), _edges_result([]))
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    result = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=uuid.uuid4(),
        nodes=[],
        edges=[edge],
        is_privileged=True,
        caller_type="mcp",
    )
    assert result is not None
    _, persisted_edges = result
    assert len(persisted_edges) == 1
    assert persisted_edges[0].condition_expression == expr


def test_edge_to_plain_dict_preserves_condition_expression() -> None:
    """``_edge_to_plain_dict`` (clone snapshot + graph-replace read path) must
    carry a conditional edge's ``condition_expression`` onto the plain data so
    clones and snapshot reads don't silently drop it (FAR-455)."""
    expr = "result.score >= `50`"
    edge = _EdgeRow(edge_type="conditional")
    edge.condition_expression = expr

    plain = _edge_to_plain_dict(edge)

    assert plain["condition_expression"] == expr
    assert plain["edge_type"] == "conditional"
    assert plain["source_node_id"] == uuid.UUID(_NODE_A)
    assert plain["target_node_id"] == uuid.UUID(_NODE_B)
