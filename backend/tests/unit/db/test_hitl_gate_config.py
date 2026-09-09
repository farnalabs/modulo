"""Unit tests: shared HITL gate-config resolver (FAR-610).

The resolver must map a gate id (``hitl_gate_<source>_<target>``) to the
config of the edge with THAT topology — from the run's snapshot graph first,
falling back to the live pipeline edges — never to an arbitrary edge by
position (the FAR-610 MCP bug approved human_only gates because the FIRST
edge of the pipeline carried no ``hitl_gate_config``).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import MultipleResultsFound

from modulo.db.crud.hitl_gate_config import (
    MSG_HUMAN_ONLY_DENY,
    MSG_HUMAN_ONLY_UNRESOLVED,
    edge_source_or_target,
    hitl_gate_exists_but_unresolved,
    human_only_denial,
    make_gate_id,
    normalize_gate_description,
    parse_hitl_gate_id,
    resolve_gate_descriptions,
    resolve_hitl_gate_config,
    snapshot_gate_config_map,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_SOURCE_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TARGET_ID = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def _gate_id() -> str:
    return make_gate_id(str(_SOURCE_ID), str(_TARGET_ID))


class TestMakeGateId:
    def test_matches_executor_format(self) -> None:
        # Byte-identical mirror of graph_cache._make_gate_id.
        assert make_gate_id("a", "b") == f"hitl_gate_{'a'}_{'b'}"

    def test_round_trips_with_parse(self) -> None:
        source, target = str(_SOURCE_ID), str(_TARGET_ID)
        assert parse_hitl_gate_id(make_gate_id(source, target)) == (source, target)


def _make_session(
    *,
    snapshot: object = None,
    edge: object = None,
    pipeline_nodes: object = None,
    claim_row: object = None,
) -> AsyncMock:
    """Session double routing SELECTs by table name to the given rows."""

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        text = str(stmt)
        if "pipeline_snapshots" in text:
            result.scalar_one_or_none.return_value = snapshot
        elif "pipeline_edges" in text:
            result.scalar_one_or_none.return_value = edge
        elif "pipelines" in text:
            result.scalar_one_or_none.return_value = pipeline_nodes
        elif "hitl_claims" in text:
            result.scalar_one_or_none.return_value = claim_row
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
        # FAR-634: the claim-stamp lookup runs FIRST (claim_row=None here —
        # a legacy unfired gate), then the snapshot hit — the live-edge
        # fallback is never queried.
        assert session.execute.await_count == 2

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
        # FAR-634: claim-stamp lookup first (None — legacy row), then the
        # live-edge fallback.
        assert session.execute.await_count == 2

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
        # Gated edges are always normal edges; uq_pipeline_edges_path includes
        # edge_type, so the fallback must filter to normal edges or two rows
        # match (normal + reject sharing one topology) and raise.
        assert "normal" in bind_values

    async def test_duplicate_topology_pair_resolves_normal_edge_config(self) -> None:
        """A normal AND a reject edge may share one (source, target) pair
        (uq_pipeline_edges_path is unique per edge_type). The fallback must
        resolve the normal edge's config instead of raising
        MultipleResultsFound on the two-row match."""
        normal_edge = MagicMock()
        normal_edge.hitl_gate_config = {"human_only": True}
        session = AsyncMock()

        async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            text = str(stmt)
            if "pipeline_snapshots" in text:
                result.scalar_one_or_none.return_value = None
            elif "pipeline_edges" in text:
                bind_values = [str(value) for value in stmt.compile().params.values()]
                if "normal" in bind_values:
                    # edge_type filter present → only the normal edge matches.
                    result.scalar_one_or_none.return_value = normal_edge
                else:
                    # No filter → both rows match → the real Result raises.
                    result.scalar_one_or_none.side_effect = MultipleResultsFound
            elif "hitl_claims" in text:
                # FAR-634: the claim-stamp lookup (no stamped row here).
                result.scalar_one_or_none.return_value = None
            elif "pipelines" in text:
                result.scalar_one_or_none.return_value = None
            else:
                raise AssertionError(f"Unexpected query in resolver: {text}")
            return result

        session.execute = AsyncMock(side_effect=_execute)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        assert result == {"human_only": True}

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


class TestResolveStampedConfigFirst:
    """FAR-634: the executor stamps the resolved config on the claim row at
    fire time; the resolver reads it FIRST (one indexed lookup) and keeps the
    snapshot/live walk as the fallback for legacy rows and never-fired gates."""

    async def test_stamped_config_returned_without_snapshot_walk(self) -> None:
        """One claim-row lookup replaces the walk entirely — the O(1) path.

        (The ``claim_row`` mock double returns the routed SELECT's scalar —
        the resolver selects the ``gate_config_json`` COLUMN, so the value is
        the config dict itself.)"""
        stamped_config = {"human_only": True, "label": "Sign-off"}
        session = _make_session(claim_row=stamped_config)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result == stamped_config
        # ONLY the claim-stamp lookup ran — no snapshot, no live edges, no
        # live pipeline nodes.
        assert session.execute.await_count == 1

    async def test_stamped_config_is_a_copy(self) -> None:
        """The resolver returns a copy — a caller mutating the dict must not
        corrupt the persisted stamp."""
        stamped_config = {"human_only": True}
        session = _make_session(claim_row=stamped_config)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        result["human_only"] = False
        assert stamped_config == {"human_only": True}

    async def test_null_stamp_falls_back_to_snapshot_walk(self) -> None:
        """Legacy row (fired before the stamp column existed): NULL config —
        the snapshot walk resolves as before."""
        config = {"human_only": True}
        snapshot = _snapshot(edges=[{"source": str(_SOURCE_ID), "target": str(_TARGET_ID), "hitl_gate_config": config}])
        session = _make_session(snapshot=snapshot, claim_row=None)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result == config
        assert session.execute.await_count == 2

    async def test_non_gate_id_skips_the_claim_stamp_lookup(self) -> None:
        """Manual-node ids never have a claim row — the stamp lookup is
        skipped entirely (submit-manual decisions pay zero extra queries)."""
        session = _make_session(snapshot=None, edge=None, claim_row=MagicMock())

        async def _forbid_claim_query(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            raise AssertionError("claim-stamp lookup must be skipped for non-gate ids")

        original_execute = session.execute

        async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            if "hitl_claims" in str(stmt):
                return await _forbid_claim_query(stmt)
            return await original_execute(stmt, *args, **kwargs)

        session.execute = AsyncMock(side_effect=_execute)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id="node-1", org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        assert result is None

    async def test_stamp_lookup_filters_run_gate_and_org(self) -> None:
        """Defence in depth: the stamp lookup filters run + gate + org."""
        session = _make_session(claim_row=None)

        await resolve_hitl_gate_config(session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run())

        stmt = session.execute.await_args_list[0].args[0]
        bind_values = [str(value) for value in stmt.compile().params.values()]
        assert str(_RUN_ID) in bind_values
        assert _gate_id() in bind_values
        assert str(_ORG_ID) in bind_values


def _hitl_node_snapshot(*, node_type: str = "hitl", source_id: uuid.UUID = _SOURCE_ID) -> MagicMock:
    """Snapshot whose graph holds a FAR-402 HITL NODE (config on the node) and
    an unconfigured edge — exactly what the persisted definition looks like
    for node-level gates (the compiler injects the config at build time)."""
    snapshot = MagicMock()
    snapshot.graph_json = {
        "nodes": [
            {
                "id": str(source_id),
                "node_type": node_type,
                "hitl_config": {"human_only": True, "label": "Sign-off"},
            }
        ],
        "edges": [{"source": str(source_id), "target": str(_TARGET_ID)}],
    }
    return snapshot


class TestResolveFromHitlNodes:
    """FAR-402 HITL nodes carry ``hitl_config`` on the NODE; the compiler
    injects it onto outgoing edges at build time, so the persisted definition
    (snapshot graph_json and live pipeline rows alike) has NO edge-level
    ``hitl_gate_config`` for node-level gates. Without the node walk these
    gates resolve None and human_only enforcement fails open."""

    async def test_resolves_node_config_from_snapshot(self) -> None:
        session = _make_session(snapshot=_hitl_node_snapshot())

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result == {"human_only": True, "label": "Sign-off"}
        # FAR-634: claim-stamp lookup first (None — legacy row), then the
        # snapshot node walk.
        assert session.execute.await_count == 2

    async def test_ignores_inert_hitl_config_on_non_hitl_node(self) -> None:
        """``hitl_config`` on a non-hitl node is ignored by the compiler —
        honouring it here would over-block, so it must not resolve."""
        session = _make_session(snapshot=_hitl_node_snapshot(node_type="agent"), edge=None)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result is None

    async def test_ignores_node_with_mismatched_topology(self) -> None:
        """The HITL node must be the gate id's SOURCE segment."""
        other_source = uuid.UUID("00000000-0000-0000-0000-00000000000c")
        session = _make_session(snapshot=_hitl_node_snapshot(source_id=other_source), edge=None)

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run()
        )

        assert result is None

    async def test_falls_back_to_live_node_config(self) -> None:
        """Snapshot missing entirely: the live pipeline's graph_nodes_json is
        consulted for the HITL-node config."""
        session = _make_session(
            snapshot=None,
            edge=None,
            pipeline_nodes=[
                {"id": str(_SOURCE_ID), "node_type": "hitl", "hitl_config": {"human_only": True}},
            ],
        )

        result = await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        assert result == {"human_only": True}
        # FAR-634: claim-stamp lookup first (None — legacy row), then live
        # edges, then live nodes.
        assert session.execute.await_count == 3

    async def test_live_node_lookup_filters_by_pipeline_and_org(self) -> None:
        session = _make_session(snapshot=None, edge=None, pipeline_nodes=None)

        await resolve_hitl_gate_config(
            session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID, run=_make_run(snapshot_id=None)
        )

        stmt = session.execute.await_args_list[2].args[0]
        bind_values = [str(value) for value in stmt.compile().params.values()]
        assert str(_PIPELINE_ID) in bind_values
        assert str(_ORG_ID) in bind_values


class TestHitlGateExistsButUnresolved:
    """Fail-closed signal: True only when a fired gate (claim row) has an
    unresolvable config. Non-gate ids (manual-node ids) short-circuit."""

    async def test_true_when_claim_row_exists(self) -> None:
        session = _make_session(claim_row=MagicMock())

        result = await hitl_gate_exists_but_unresolved(session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID)

        assert result is True

    async def test_false_when_no_claim_row(self) -> None:
        session = _make_session(claim_row=None)

        result = await hitl_gate_exists_but_unresolved(session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID)

        assert result is False

    async def test_non_gate_id_short_circuits_without_query(self) -> None:
        """Manual-node ids can never have a claim row — no query at all, so the
        fail-closed check cannot over-block manual output delivery."""
        session = _make_session(claim_row=MagicMock())

        result = await hitl_gate_exists_but_unresolved(session, run_id=_RUN_ID, gate_id="node-1", org_id=_ORG_ID)

        assert result is False
        assert session.execute.await_count == 0

    async def test_claim_query_filters_run_gate_and_org(self) -> None:
        session = _make_session(claim_row=None)

        await hitl_gate_exists_but_unresolved(session, run_id=_RUN_ID, gate_id=_gate_id(), org_id=_ORG_ID)

        stmt = session.execute.await_args.args[0]
        bind_values = [str(value) for value in stmt.compile().params.values()]
        assert str(_RUN_ID) in bind_values
        assert _gate_id() in bind_values
        assert str(_ORG_ID) in bind_values


class TestHumanOnlyDenial:
    """Pure deny-policy verdict shared by the REST decision routes and the
    MCP ``review_hitl`` tool (FAR-610 review) — browser credentials always
    pass, ``human_only`` configs deny, unresolvable FIRED gates fail closed."""

    def test_browser_credential_always_allowed(self) -> None:
        # First check, no other logic: even a human_only config cannot block.
        verdict = human_only_denial({"human_only": True}, non_browser_credential=False, gate_fired=True)
        assert verdict is None

    def test_api_key_denied_on_human_only(self) -> None:
        verdict = human_only_denial({"human_only": True}, non_browser_credential=True, gate_fired=True)
        assert verdict == MSG_HUMAN_ONLY_DENY

    def test_api_key_allowed_on_non_human_only(self) -> None:
        verdict = human_only_denial({"human_only": False}, non_browser_credential=True, gate_fired=True)
        assert verdict is None

    def test_unresolvable_fired_gate_fails_closed(self) -> None:
        verdict = human_only_denial(None, non_browser_credential=True, gate_fired=True)
        assert verdict == MSG_HUMAN_ONLY_UNRESOLVED

    def test_unresolvable_not_fired_allows(self) -> None:
        verdict = human_only_denial(None, non_browser_credential=True, gate_fired=False)
        assert verdict is None


class TestSnapshotGateConfigMap:
    """FAR-613: the whole-snapshot gate-config walk behind the pending-gate
    description maps — covers BOTH gate shapes with ONE walk."""

    def test_covers_edge_gate_shape(self) -> None:
        config = {"label": "Sign-off", "description": "Human approves the release."}
        graph = {
            "nodes": [],
            "edges": [{"source": str(_SOURCE_ID), "target": str(_TARGET_ID), "hitl_gate_config": config}],
        }
        assert snapshot_gate_config_map(graph) == {_gate_id(): config}

    def test_covers_node_gate_shape_over_outgoing_edges(self) -> None:
        config = {"description": "Human confirms the resolution."}
        graph = {
            "nodes": [{"id": str(_SOURCE_ID), "node_type": "hitl", "hitl_config": config}],
            "edges": [{"source": str(_SOURCE_ID), "target": str(_TARGET_ID)}],
        }
        assert snapshot_gate_config_map(graph) == {_gate_id(): config}

    def test_node_gate_does_not_shadow_a_matching_edge_gate(self) -> None:
        edge_config = {"description": "Edge-level description wins."}
        node_config = {"description": "Node-level description."}
        graph = {
            "nodes": [{"id": str(_SOURCE_ID), "node_type": "hitl", "hitl_config": node_config}],
            "edges": [{"source": str(_SOURCE_ID), "target": str(_TARGET_ID), "hitl_gate_config": edge_config}],
        }
        assert snapshot_gate_config_map(graph) == {_gate_id(): edge_config}

    def test_inert_hitl_config_on_non_hitl_node_ignored(self) -> None:
        graph = {
            "nodes": [{"id": str(_SOURCE_ID), "node_type": "agent", "hitl_config": {"description": "inert"}}],
            "edges": [{"source": str(_SOURCE_ID), "target": str(_TARGET_ID)}],
        }
        assert not snapshot_gate_config_map(graph)


class TestNormalizeGateDescription:
    def test_strips_usable_description(self) -> None:
        config = {"description": "  Human approves the release.  "}
        assert normalize_gate_description(config) == "Human approves the release."

    def test_blank_or_missing_or_non_string_maps_none(self) -> None:
        assert normalize_gate_description({"description": "   "}) is None
        assert normalize_gate_description({}) is None
        assert normalize_gate_description({"description": 42}) is None
        assert normalize_gate_description(None) is None


class TestResolveGateDescriptions:
    """FAR-613: batched per-gate description resolution for the org-level
    pending surfaces (REST + MCP) — two IN queries, never per-gate walks."""

    def _make_batched_session(self, run_rows: list, snapshot_rows: list) -> AsyncMock:
        async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
            result = MagicMock()
            text = str(stmt)
            if "pipeline_snapshots" in text:
                result.all = MagicMock(return_value=snapshot_rows)
            elif "runs" in text:
                result.all = MagicMock(return_value=run_rows)
            else:
                raise AssertionError(f"Unexpected query in resolver: {text}")
            return result

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=_execute)
        return session

    def _gate(self, run_id: uuid.UUID, gate_id: str) -> MagicMock:
        gate = MagicMock()
        gate.run_id = run_id
        gate.gate_id = gate_id
        return gate

    async def test_resolves_description_via_batched_queries(self) -> None:
        edge_gate_id = _gate_id()
        graph = {
            "nodes": [{"id": str(_SOURCE_ID), "node_type": "hitl", "hitl_config": {"description": "Node gate why."}}],
            "edges": [
                {
                    "source": str(_SOURCE_ID),
                    "target": str(_TARGET_ID),
                    "hitl_gate_config": {"description": "Edge gate why."},
                }
            ],
        }
        gate = self._gate(_RUN_ID, edge_gate_id)
        session = self._make_batched_session(
            run_rows=[(_RUN_ID, _SNAPSHOT_ID)],
            snapshot_rows=[(_SNAPSHOT_ID, graph)],
        )

        result = await resolve_gate_descriptions(session, gates=[gate], org_id=_ORG_ID)

        assert result == {(_RUN_ID, edge_gate_id): "Edge gate why."}

    async def test_missing_snapshot_maps_none(self) -> None:
        gate = self._gate(_RUN_ID, _gate_id())
        session = self._make_batched_session(run_rows=[(_RUN_ID, None)], snapshot_rows=[])

        result = await resolve_gate_descriptions(session, gates=[gate], org_id=_ORG_ID)

        assert result == {(_RUN_ID, gate.gate_id): None}

    async def test_gate_without_usable_description_maps_none(self) -> None:
        gate = self._gate(_RUN_ID, _gate_id())
        graph = {
            "nodes": [],
            "edges": [{"source": str(_SOURCE_ID), "target": str(_TARGET_ID), "hitl_gate_config": {"label": "no desc"}}],
        }
        session = self._make_batched_session(run_rows=[(_RUN_ID, _SNAPSHOT_ID)], snapshot_rows=[(_SNAPSHOT_ID, graph)])

        result = await resolve_gate_descriptions(session, gates=[gate], org_id=_ORG_ID)

        assert result == {(_RUN_ID, gate.gate_id): None}

    async def test_empty_gates_short_circuits_without_queries(self) -> None:
        session = self._make_batched_session(run_rows=[], snapshot_rows=[])

        result = await resolve_gate_descriptions(session, gates=[], org_id=_ORG_ID)

        assert result == {}
        assert session.execute.await_count == 0
