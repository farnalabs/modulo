"""Unit tests for FAR-795: the node-start DAG-ancestor refs injection wiring.

The pure collector (``collect_injected_refs``) is tested in
``test_ancestor_ref_injection.py``. This module tests the WIRING:

* ``_compute_dag_ancestor_ids`` — compile-time ancestor closure over the
  graph_json forwarding edges (diamond scoping, reject-edge exclusion).
* ``make_node_fn`` — the FULL node-start injection: a declaring node receives
  create-time refs PLUS its DAG ancestors' emissions (from accumulated
  ``state["artifacts"]``), parallel branches and skipped/failed attempts
  contribute per their status, retries fold to one union, the unified cap
  applies over the union, and an assembly failure degrades to an EMPTY
  injection plus a counter event — the node itself still completes.

No DB:
* the ancestor sets come from ``_compute_dag_ancestor_ids`` over a literal
  node/edge graph (the same compile-time shape ``build_graph_from_json`` feeds
  the node fns);
* ``state`` is a literal LangGraph-state dict of the shape the executor
  accumulates;
* the conformance gate and model invocation are stubbed at the node_runner
  module boundary (the wiring under test is the injection, not the gate/model).

Agent Return Contract: the injection path never writes ``outputs_json`` or
``node_telemetry_json`` — the fail-open counter goes through
``notify_refs_event`` (dedicated telemetry channel), and the read paths are
pure over ``state`` (asserted below).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from modulo.core.pipeline_engine.graph_cache import _compute_dag_ancestor_ids
from modulo.core.pipeline_engine.node_runner import (
    _fold_ancestor_emission_refs,
    make_node_fn,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref(kind: str, ref: str, source: str) -> dict[str, object]:
    return {"kind": kind, "ref": ref, "source": source}


def _artifact(node_id: str, status: str, output: dict[str, Any] | str) -> dict[str, Any]:
    return {"node_id": node_id, "status": status, "output": output}


def _diamond_ancestors() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Compile-time ancestor sets for the small graph a -> b -> (d, c).

    ``a -> b -> d`` is the data path into ``d``; ``b -> c`` is a parallel
    branch that runs alongside ``d`` — ``c`` contributes NOTHING to ``d``'s
    ancestry even though it shares the fork at ``b``.
    """
    by_node = _compute_dag_ancestor_ids(
        ["a", "b", "c", "d"],
        [{"source": "a", "target": "b"}, {"source": "b", "target": "d"}, {"source": "b", "target": "c"}],
    )
    return by_node["d"], by_node["b"], by_node["c"]


def _declaring_node_def() -> dict[str, Any]:
    return {
        "id": "d",
        "model_backend_id": "stub",
        "prompt_template": "refs: {{ work_item_refs | length }}",
        "input_schema_json": {"properties": {"work_item_refs": {"type": "array"}}},
    }


def _declaring_state(
    create_refs: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_context": {"cancelled": False, "input": {"_work_item_refs": create_refs or []}},
        "artifacts": artifacts or [],
    }


# A declaring state whose artifact ledger matches the diamond graph:
# ancestor a emitted a PR ref, sibling-ancestor b a Linear ref, PARALLEL node
# c a Slack ref that must never reach d.
_D_ANC, _B_ANC, _C_ANC = _diamond_ancestors()
_COMPLETION_ARTIFACTS = [
    _artifact("a", "completed", {"work_item_refs": [_ref("github_pr", "12", "agent")]}),
    _artifact("b", "completed", {"work_item_refs": [_ref("linear", "X-2", "derived")]}),
    # Parallel branch (same superstep as b, joined at d): completed but NOT an
    # ancestor of d through b's edge — the collect must exclude it.
    _artifact("c", "completed", {"work_item_refs": [_ref("slack", "off-branch", "agent")]}),
]


# ---------------------------------------------------------------------------
# compile-time ancestor computation
# ---------------------------------------------------------------------------


class TestComputeDagAncestorIds:
    def test_diamond_computes_transitive_ancestors_without_self(self) -> None:
        d_anc, b_anc, c_anc = _diamond_ancestors()
        assert d_anc == frozenset({"a", "b"})
        assert b_anc == frozenset({"a"})
        assert c_anc == frozenset({"a", "b"})

    def test_reject_edges_are_excluded_from_ancestry(self) -> None:
        # e --reject--> d would otherwise misfold a correction kick-back into a
        # data dependency; the forwarding edge from a is the only real link.
        by_node = _compute_dag_ancestor_ids(
            ["a", "d", "e"],
            [{"source": "a", "target": "d"}, {"source": "e", "target": "d", "type": "reject"}],
        )
        assert by_node["d"] == frozenset({"a"})

    def test_cycle_terminates_and_makes_members_mutual_ancestors(self) -> None:
        cycle_by_node = _compute_dag_ancestor_ids(
            ["x", "y"],
            [{"source": "x", "target": "y"}, {"source": "y", "target": "x"}],
        )
        assert cycle_by_node["x"] == frozenset({"y"})
        assert cycle_by_node["y"] == frozenset({"x"})

    def test_orphan_edges_and_unknown_ids_are_tolerated(self) -> None:
        by_node = _compute_dag_ancestor_ids(
            ["a"],
            [{"source": "ghost", "target": "a"}, {"source": "a", "target": "also-ghost"}],
        )
        assert by_node["a"] == frozenset({"ghost"})


# ---------------------------------------------------------------------------
# artifact fold
# ---------------------------------------------------------------------------


class TestFoldAncestorEmissionRefs:
    def test_completed_and_executed_status_contribute(self) -> None:
        state = _declaring_state(
            artifacts=[
                _artifact("a", "completed", {"work_item_refs": [_ref("github_pr", "1", "agent")]}),
                _artifact("b", "executed", {"work_item_refs": [_ref("linear", "2", "derived")]}),
            ]
        )
        folded = _fold_ancestor_emission_refs(state)
        assert folded["a"] == [_ref("github_pr", "1", "agent")]
        assert folded["b"] == [_ref("linear", "2", "derived")]

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        malformed_state = _declaring_state(
            artifacts=[
                _artifact(
                    "a", "completed", {"work_item_refs": ["garbage", {"kind": "jira"}, _ref("jira", "OK", "agent")]}
                ),
            ]
        )
        folded = _fold_ancestor_emission_refs(malformed_state)
        assert folded["a"] == [_ref("jira", "OK", "agent")]

    def test_no_artifacts_or_wrong_shape_yields_empty(self) -> None:
        assert not _fold_ancestor_emission_refs({})
        assert not _fold_ancestor_emission_refs({"artifacts": None})
        assert not _fold_ancestor_emission_refs({"artifacts": ["still-not-a-dict"]})


# ---------------------------------------------------------------------------
# full node-start wiring (make_node_fn end-to-end)
# ---------------------------------------------------------------------------


async def _invoke_node(
    create_refs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    ancestor_ids: frozenset[str],
    cap: int = 100,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Invoke the REAL compiled node fn for the declaring 'd' node.

    Returns ``@`` the work_item_refs kwarg actually passed into the prompt
    render, and the node's returned state update.
    """
    captured: dict[str, Any] = {}

    def _recording_render(**kwargs: Any) -> tuple[str, str]:
        captured["work_item_refs"] = kwargs.get("work_item_refs")
        return "rendered", "default"

    node_def = _declaring_node_def()
    fn = make_node_fn(node_def, dag_ancestor_ids=ancestor_ids)
    state = _declaring_state(create_refs=create_refs, artifacts=artifacts)
    with (
        patch("modulo.core.pipeline_engine.node_runner._run_conformance_gate", new=AsyncMock(return_value=None)),
        patch("modulo.core.pipeline_engine.node_runner.work_item_refs_cap", lambda: cap),
        patch("modulo.core.pipeline_engine.node_runner._invoke_node_model", new=AsyncMock(return_value="out")),
        patch("modulo.core.pipeline_engine.node_runner._render_agent_prompt", new=_recording_render),
    ):
        result = await fn(state)
    return captured.get("work_item_refs"), result


class TestUnionInjectionAtNodeStart:
    async def test_downstream_node_gets_create_time_union_ancestor_refs(self) -> None:
        create_refs, result = await _invoke_node(
            create_refs=[_ref("jira", "FAR-1", "caller")],
            artifacts=_COMPLETION_ARTIFACTS,
            ancestor_ids=_D_ANC,
        )
        assert [(r["kind"], r["ref"]) for r in create_refs or []] == [
            ("jira", "FAR-1"),
            ("github_pr", "12"),
            ("linear", "X-2"),
        ]
        # The node itself still ran through to its artifact.
        assert result["artifacts"][0]["node_id"] == "d"
        assert result["artifacts"][0]["status"] == "completed"

    async def test_parallel_branch_refs_are_not_injected(self) -> None:
        refs, _ = await _invoke_node(
            create_refs=[],
            artifacts=_COMPLETION_ARTIFACTS,
            ancestor_ids=_D_ANC,
            cap=100,
        )
        assert all(r["ref"] != "off-branch" for r in refs or [])

    async def test_skipped_ancestor_contributes_nothing(self) -> None:
        refs, _ = await _invoke_node(
            create_refs=[],
            artifacts=[
                # The ancestor's only attempt was skipped — no completed
                # attempt exists to fold.
                _artifact("a", "skipped", {"work_item_refs": [_ref("github_pr", "12", "agent")]}),
            ],
            ancestor_ids=frozenset({"a"}),
        )
        assert refs == []

    async def test_ancestor_retry_does_not_duplicate(self) -> None:
        refs, _ = await _invoke_node(
            create_refs=[],
            artifacts=[
                _artifact("a", "completed", {"work_item_refs": [_ref("github_pr", "12", "agent")]}),
                # The ancestor's correction re-run completed again and emitted
                # the same ref — the fold unions, the collector dedupes.
                _artifact("a", "completed", {"work_item_refs": [_ref("github_pr", "12", "agent")]}),
            ],
            ancestor_ids=frozenset({"a"}),
        )
        assert len(refs or []) == 1

    async def test_unified_cap_applies_over_the_union(self) -> None:
        # One caller-ranked create-time ref and two agent-ranked supplies
        # (create-time slack + ancestor PR), cap=2: agent entries drop FIRST
        # (rank ascending), so the caller survives alongside the earlier
        # agent entry (position tie-break).
        refs, _ = await _invoke_node(
            create_refs=[_ref("slack", "notifies", "agent"), _ref("jira", "SC", "caller")],
            artifacts=[
                _artifact("a", "completed", {"work_item_refs": [_ref("github_pr", "12", "agent")]}),
            ],
            ancestor_ids=frozenset({"a"}),
            cap=2,
        )
        assert [(r["kind"], r["ref"]) for r in refs or []] == [("jira", "SC"), ("github_pr", "12")]

    async def test_unthreaded_caller_keeps_create_time_only(self) -> None:
        # A call path that never got the compile-time ancestry (e.g. a node
        # fn built outside build_graph_from_json) degrades to the create-time
        # supply — never a raise, never an ancestor leak.
        refs, _ = await _invoke_node(
            create_refs=[_ref("jira", "KEEP", "caller")],
            artifacts=_COMPLETION_ARTIFACTS,
            ancestor_ids=frozenset(),
        )
        assert [(r["kind"], r["ref"]) for r in refs or []] == [("jira", "KEEP")]


class TestAssemblyFailOpen:
    async def test_assembly_failure_injects_empty_and_counts(self) -> None:
        counter_events: list[str] = []
        with (
            patch("modulo.core.pipeline_engine.node_runner.collect_injected_refs", side_effect=RuntimeError("boom")),
            patch(
                "modulo.core.pipeline_engine.node_runner.notify_refs_event",
                side_effect=lambda name, *a, **kw: counter_events.append(name),
            ),
        ):
            refs, result = await _invoke_node(
                create_refs=[_ref("jira", "FAR-1", "caller")],
                artifacts=_COMPLETION_ARTIFACTS,
                ancestor_ids=_D_ANC,
            )
        assert refs == []
        assert counter_events == ["ancestor_refs_assembly_failed"]
        # The node STILL STARTS — it completed with its artifact, the
        # injection never failed the node.
        assert result["artifacts"][0]["status"] == "completed"

    async def test_state_is_never_mutated_by_the_injection_path(self) -> None:
        # The wiring is a pure READ over LangGraph state — the Agent Return
        # Contract columns (outputs_json / node_telemetry_json) are serialised
        # FROM this state, so the injection path itself never writes anywhere:
        # the state dict's snapshot must be identical after the node runs.
        state = _declaring_state(create_refs=[_ref("jira", "FAR-1", "caller")], artifacts=_COMPLETION_ARTIFACTS)
        snapshot = repr(state)
        fn = make_node_fn(_declaring_node_def(), dag_ancestor_ids=_D_ANC)
        with (
            patch("modulo.core.pipeline_engine.node_runner._run_conformance_gate", new=AsyncMock(return_value=None)),
            patch("modulo.core.pipeline_engine.node_runner._invoke_node_model", new=AsyncMock(return_value="out")),
        ):
            result = await fn(state)
        assert repr(state) == snapshot
        # The node returns its artifact update without writing into the
        # caller's state dict (LangGraph applies the update itself).
        assert result["artifacts"][0]["status"] == "completed"
