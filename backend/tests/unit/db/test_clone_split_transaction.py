"""Clone split-transaction tests (hitl-gate-removal-guard-plan.md v19 §3 item 3).

Verifies the step-(a) short read transaction / step-(b) slow clone-write split:
- the FOR SHARE read happens on a separate session that commits before the
  main session does any clone work (liveness),
- step (b) uses the plain-data snapshot captured in step (a), so a concurrent
  write cannot tear the clone (torn-read correctness),
- all cloned gated edges emit ONE batched edge_created_with_gate audit event.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.pipeline import _clone_edges, clone_pipeline

pytestmark = pytest.mark.asyncio(loop_scope="module")

_NODE_A = "00000000-0000-0000-0000-0000000000a1"
_NODE_B = "00000000-0000-0000-0000-0000000000a2"


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _BeginCM:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("read_txn_begin")

    async def __aexit__(self, *args: object) -> None:
        self._events.append("read_txn_commit")


def _scalar_result(row: object) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = row
    return r


def _scalars_result(rows: list[object]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = list(rows)
    return r


def _make_read_session(
    *,
    events: list[str],
    on_held: Any,
    source: _Row,
    edges: list[_Row],
    snapshots: list[_Row],
    pins_by_snap: dict[str, list[_Row]],
) -> AsyncMock:
    results = [_scalar_result(source), _scalars_result(edges), _scalars_result(snapshots)]
    results.extend(_scalars_result(pins_by_snap.get(str(snap.id), [])) for snap in snapshots)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    session.begin = MagicMock(return_value=_BeginCM(events))
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    # async with factory() as read_session: the context manager must hand back
    # the SAME object so the code's read_session.begin()/execute resolve to
    # this mock (AsyncMock's default __aenter__ returns a fresh mock).
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    # The step-(a)-held hook is invoked by the code between the source lock and
    # the read transaction's commit.
    return session


def _read_factory(read_session: AsyncMock) -> Any:
    def _factory() -> AsyncMock:
        return read_session

    return _factory


def _make_source() -> _Row:
    return _Row(
        id=uuid.uuid4(),
        name="Original Pipeline",
        description="desc",
        visibility="org",
        owner_team_id=None,
        max_concurrent_runs=5,
        lock_wait_timeout_seconds=300,
        node_timeout_seconds=300,
        run_context_defaults={},
        graph_nodes_json=[{"id": _NODE_A, "node_type": "agent"}],
        default_autonomy_level="manual_approval",
        stale_run_timeout_minutes=30,
    )


def _make_main_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    return session


def _gated_edge() -> _Row:
    return _Row(
        id=uuid.uuid4(),
        source_node_id=uuid.UUID(_NODE_A),
        target_node_id=uuid.UUID(_NODE_B),
        edge_type="normal",
        source_port="out",
        target_port="in",
        hitl_gate_config={"human_only": True},
    )


# ---------------------------------------------------------------------------
# Liveness: step (a) commits before any step-(b) write
# ---------------------------------------------------------------------------


async def test_step_a_commits_before_step_b_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)
    events: list[str] = []
    source = _make_source()
    read_session = _make_read_session(
        events=events,
        on_held=None,
        source=source,
        edges=[_gated_edge()],
        snapshots=[],
        pins_by_snap={},
    )

    async def on_step_a_committed() -> None:
        assert "read_txn_commit" in events, "step (a) must commit before step (b) starts"
        events.append("step_a_committed")

    main_session = _make_main_session()
    factory = _read_factory(read_session)

    cloned = await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
        _on_step_a_committed=on_step_a_committed,
    )

    assert cloned is not None
    # The main session (caller's) never queried the source: no lock is held on
    # the caller's connection during the slow clone work.
    main_session.execute.assert_not_awaited()
    assert events[-1] == "step_a_committed"


async def test_step_a_held_fires_while_read_transaction_open() -> None:
    events: list[str] = []
    source = _make_source()
    read_session = _make_read_session(
        events=events,
        on_held=None,
        source=source,
        edges=[],
        snapshots=[],
        pins_by_snap={},
    )

    async def on_held() -> None:
        assert "read_txn_begin" in events
        assert "read_txn_commit" not in events, "the FOR SHARE lock is still held"
        events.append("step_a_held")

    factory = _read_factory(read_session)
    main_session = _make_main_session()

    await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
        _on_step_a_held=on_held,
    )
    assert "step_a_held" in events
    assert events.index("step_a_held") < events.index("read_txn_commit")


# ---------------------------------------------------------------------------
# Torn-read correctness: step (b) uses the step-(a) plain-data snapshot
# ---------------------------------------------------------------------------


async def test_step_b_uses_step_a_snapshot_not_a_live_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)
    source = _make_source()
    events: list[str] = []

    async def on_step_a_committed() -> None:
        # A concurrent replace_pipeline_graph commits AFTER step (a) released
        # the FOR SHARE lock. The clone must still use the step-(a) plain-data
        # snapshot, never a live re-read of the source.
        source.name = "Concurrent Rename"

    read_session = _make_read_session(
        events=events,
        on_held=None,
        source=source,
        edges=[_gated_edge()],
        snapshots=[],
        pins_by_snap={},
    )
    factory = _read_factory(read_session)
    main_session = _make_main_session()

    await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
        _on_step_a_committed=on_step_a_committed,
    )

    # The main session (caller's) never re-reads the source: no lock dependency.
    main_session.execute.assert_not_awaited()
    added_pipelines = [c for c in main_session.add.call_args_list if type(c.args[0]).__name__ == "Pipeline"]
    assert added_pipelines, "expected the cloned Pipeline to be added on the main session"
    cloned = added_pipelines[0].args[0]
    # The clone used the step-(a) snapshot (name "Original Pipeline"), NOT the
    # concurrent rename.
    assert cloned.name == "Copy of Original Pipeline"
    assert cloned.graph_nodes_json == [{"id": _NODE_A, "node_type": "agent"}]


# ---------------------------------------------------------------------------
# Batched edge_created_with_gate audit
# ---------------------------------------------------------------------------


async def test_clone_emits_one_batched_gate_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source()
    ungated = _Row(
        id=uuid.uuid4(),
        source_node_id=uuid.UUID(_NODE_A),
        target_node_id=uuid.UUID(_NODE_B),
        edge_type="normal",
        source_port="out",
        target_port="in",
        hitl_gate_config=None,
    )
    edges = [_gated_edge(), _gated_edge(), ungated]
    read_session = _make_read_session(
        events=[],
        on_held=None,
        source=source,
        edges=edges,
        snapshots=[],
        pins_by_snap={},
    )
    factory = _read_factory(read_session)
    main_session = _make_main_session()

    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
    )

    audit_calls = list(audit.call_args_list)
    assert audit_calls, "expected at least the edge_created_with_gate audit event"
    gate_events = [c for c in audit_calls if c.kwargs.get("event_type") == "edge_created_with_gate"]
    assert len(gate_events) == 1, "the batched audit event must be emitted exactly once"
    edge_ids = gate_events[0].kwargs["payload_json"]["edge_ids"]
    assert len(edge_ids) == 2, f"expected 2 cloned gated edges, got {len(edge_ids)}"


async def test_clone_preserves_non_default_edge_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)
    source = _make_source()
    custom_edge = _Row(
        id=uuid.uuid4(),
        source_node_id=uuid.UUID(_NODE_A),
        target_node_id=uuid.UUID(_NODE_B),
        edge_type="normal",
        hitl_gate_config=None,
        source_port="custom_out",
        target_port="custom_in",
    )
    read_session = _make_read_session(
        events=[],
        on_held=None,
        source=source,
        edges=[custom_edge],
        snapshots=[],
        pins_by_snap={},
    )
    factory = _read_factory(read_session)
    main_session = _make_main_session()

    await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
    )

    added_edges = [c.args[0] for c in main_session.add.call_args_list if type(c.args[0]).__name__ == "PipelineEdge"]
    assert added_edges, "expected the cloned PipelineEdge to be added on the main session"
    cloned_edge = added_edges[0]
    # The clone must copy the source edge's real (non-default) ports, not fall
    # back to the legacy "out"/"in" defaults.
    assert cloned_edge.source_port == "custom_out"
    assert cloned_edge.target_port == "custom_in"


async def test_clone_edges_coalesces_null_ports_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove-the-fix for the deploy NOT NULL bug.

    A ``pipeline_edges`` row whose ``source_port``/``target_port`` is NULL (or an
    edge dict carrying ``source_port: null``) is the exact shape that tripped the
    NOT NULL violation. ``_clone_edges`` must coalesce the missing value to the
    legacy ``"out"``/``"in"`` defaults rather than writing an explicit ``None``.

    This test fails against the pre-fix ``edge.get("source_port", "out")`` (the
    default only fires when the key is *missing*, so a present ``None`` flows
    through) and passes with ``edge.get("source_port") or "out"``.
    """
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", AsyncMock())
    session = _make_main_session()
    edges = [
        {
            "source_node_id": uuid.UUID(_NODE_A),
            "target_node_id": uuid.UUID(_NODE_B),
            "edge_type": "normal",
            "hitl_gate_config": None,
            "source_port": None,
            "target_port": None,
        }
    ]
    await _clone_edges(
        session,
        edges,
        source_id=uuid.uuid4(),
        cloned_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
    )
    added = [c.args[0] for c in session.add.call_args_list if type(c.args[0]).__name__ == "PipelineEdge"]
    assert added, "expected the cloned PipelineEdge to be added"
    cloned_edge = added[0]
    assert cloned_edge.source_port == "out"
    assert cloned_edge.target_port == "in"


async def test_clone_falls_back_to_default_ports_when_source_ports_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end regression: cloning a pipeline whose edges have NULL ports
    must yield the legacy ``"out"``/``"in"`` defaults, never ``None``.

    This reproduces the production bug shape where a migrated ``pipeline_edges``
    row carries NULL ``source_port``/``target_port``.
    """
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)
    source = _make_source()
    null_edge = _Row(
        id=uuid.uuid4(),
        source_node_id=uuid.UUID(_NODE_A),
        target_node_id=uuid.UUID(_NODE_B),
        edge_type="normal",
        hitl_gate_config=None,
        source_port=None,
        target_port=None,
    )
    read_session = _make_read_session(
        events=[],
        on_held=None,
        source=source,
        edges=[null_edge],
        snapshots=[],
        pins_by_snap={},
    )
    factory = _read_factory(read_session)
    main_session = _make_main_session()

    await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
    )

    added_edges = [c.args[0] for c in main_session.add.call_args_list if type(c.args[0]).__name__ == "PipelineEdge"]
    assert added_edges, "expected the cloned PipelineEdge to be added"
    cloned_edge = added_edges[0]
    assert cloned_edge.source_port == "out"
    assert cloned_edge.target_port == "in"


async def test_clone_no_gate_edges_emits_no_gate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source()
    edges = [
        _Row(
            id=uuid.uuid4(),
            source_node_id=uuid.UUID(_NODE_A),
            target_node_id=uuid.UUID(_NODE_B),
            edge_type="normal",
            source_port="out",
            target_port="in",
            hitl_gate_config=None,
        )
    ]
    read_session = _make_read_session(
        events=[],
        on_held=None,
        source=source,
        edges=edges,
        snapshots=[],
        pins_by_snap={},
    )
    factory = _read_factory(read_session)
    main_session = _make_main_session()
    audit = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.pipeline.append_audit_event", audit)

    await clone_pipeline(
        main_session,
        org_id=uuid.uuid4(),
        pipeline_id=source.id,
        account_id=uuid.uuid4(),
        _read_session_factory=factory,
    )
    gate_events = [c for c in audit.call_args_list if c.kwargs.get("event_type") == "edge_created_with_gate"]
    assert not gate_events
