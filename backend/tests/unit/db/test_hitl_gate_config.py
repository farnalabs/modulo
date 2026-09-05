"""Unit tests: shared HITL gate-config resolver (FAR-610).

The resolver must map a gate id (``hitl_gate_<source>_<target>``) to the
config of the edge with THAT topology — from the run's snapshot graph first,
falling back to the live pipeline edges — never to an arbitrary edge by
position (the FAR-610 MCP bug approved human_only gates because the FIRST
edge of the pipeline carried no ``hitl_gate_config``).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.hitl_gate_config import (
    edge_source_or_target,
    parse_hitl_gate_id,
    resolve_hitl_gate_config,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_SOURCE_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TARGET_ID = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def _gate_id() -> str:
    return f"hitl_gate_{_SOURCE_ID}_{_TARGET_ID}"


def _make_session(
    *,
    snapshot: object = None,
    edge: object = None,
) -> AsyncMock:
    """Session double routing SELECTs by table name to the given rows."""

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        text = str(stmt)
        if "pipeline_snapshots" in text:
            result.scalar_one_or_none.return_value = snapshot
        elif "pipeline_edges" in text:
            result.scalar_one_or_none.return_value = edge
        else:
            raise AssertionError(f"Unexpected query in resolver: {text}")
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_run(*, snapshot_id: uuid.UUID | None = _SNAPSHOT_ID) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _PIPELINE_ID
    run.organisation_id = _ORG_ID
    run.snapshot_id = snapshot_id
    return run


def _snapshot(*, edges: list) -> MagicMock:
    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [], "edges": edges}
    return snapshot


class TestParseHitlGateId:
    def test_parses_uuid_topology(self) -> None:
        assert parse_hitl_gate_id(_gate_id()) == (str(_SOURCE_ID), str(_TARGET_ID))

    def test_parses_short_node_names(self) -> None:
        assert parse_hitl_gate_id("hitl_gate_planner_deploy") == ("planner", "deploy")

    def test_rejects_non_gate_id(self) -> None:
        assert parse_hitl_gate_id("gate-1") is None

    def test_rejects_single_segment(self) -> None:
        assert parse_hitl_gate_id("hitl_gate_onlyone") is None

    def test_rejects_three_segments(self) -> None:
        # Node ids are UUIDs (no underscores); three segments is ambiguous.
        assert parse_hitl_gate_id("hitl_gate_a_b_c") is None


class TestEdgeSourceOrTarget:
    def test_canonical_keys(self) -> None:
        edge = {"source": "a", "target": "b"}
        assert edge_source_or_target(edge, "source") == "a"
        assert edge_source_or_target(edge, "target") == "b"

    def test_persisted_keys(self) -> None:
        edge = {"source_node_id": "a", "target_node_id": "b"}
        assert edge_source_or_target(edge, "source") == "a"
        assert edge_source_or_target(edge, "target") == "b"

    def test_missing_keys_returns_none(self) -> None:
        assert edge_source_or_target({}, "source") is None


class TestResolveFromSnapshot:
    async def test_returns_config_of_matching_edge(self) -> None:
        config = {"human_only": True, "label": "Sign-off"}
        snapshot = _snapshot(
            edges=[
                {"source": str(_SOURCE_ID), "target": str(_TARGET_ID), "hitl_gate_config": config},
            ]
        )
        session = _make_session(snapshot=snapshot)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result == config
        # Snapshot hit → the live-edge fallback is never queried.
        assert session.execute.await_count == 1

    async def test_matches_persisted_node_key_style(self) -> None:
        config = {"human_only": False}
        snapshot = _snapshot(
            edges=[
                {"source_node_id": str(_SOURCE_ID), "target_node_id": str(_TARGET_ID), "hitl_gate_config": config},
            ]
        )
        session = _make_session(snapshot=snapshot)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result == config

    async def test_ignores_edges_without_config(self) -> None:
        snapshot = _snapshot(
            edges=[
                {"source": str(_SOURCE_ID), "target": str(_TARGET_ID)},
            ]
        )
        session = _make_session(snapshot=snapshot, edge=None)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result is None

    async def test_falls_back_to_live_edges_when_snapshot_edge_unconfigured(self) -> None:
        """Legacy snapshot (no config on the edge) → live-edge fallback by topology."""
        snapshot = _snapshot(
            edges=[
                {"source": str(_SOURCE_ID), "target": str(_TARGET_ID)},
                {"source": "unrelated-1", "target": "unrelated-2", "hitl_gate_config": {"human_only": True}},
            ]
        )
        live_edge = MagicMock()
        live_edge.hitl_gate_config = {"human_only": True}
        session = _make_session(snapshot=snapshot, edge=live_edge)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result == {"human_only": True}


class TestResolveFallbacks:
    async def test_falls_back_to_live_edges_without_snapshot(self) -> None:
        live_edge = MagicMock()
        live_edge.hitl_gate_config = {"human_only": True}
        session = _make_session(snapshot=None, edge=live_edge)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        assert result == {"human_only": True}
        assert session.execute.await_count == 1

    async def test_live_edge_query_filters_by_topology(self) -> None:
        """The fallback queries source AND target, not the pipeline's first edge."""
        live_edge = MagicMock()
        live_edge.hitl_gate_config = {"human_only": False}
        session = _make_session(snapshot=None, edge=live_edge)

        await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        stmt = session.execute.await_args.args[0]
        bind_values = [str(value) for value in stmt.compile().params.values()]
        assert str(_SOURCE_ID) in bind_values
        assert str(_TARGET_ID) in bind_values
        assert str(_PIPELINE_ID) in bind_values
        assert str(_ORG_ID) in bind_values

    async def test_returns_none_when_run_missing(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        resolved = await resolve_hitl_gate_config(session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID)

        assert resolved is None

    async def test_returns_none_for_non_gate_id(self) -> None:
        session = _make_session(snapshot=None, edge=None)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id="node-1", org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        assert result is None
