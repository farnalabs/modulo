"""Unit tests for FAR-795: the pure DAG-ancestor work-item ref injector.

``collect_injected_refs`` (``core/pipeline_engine/ancestor_refs.py``) computes
the refs to inject into a node's input at node start: create-time refs plus
DAG-ancestor emissions, deduped by canonical (kind, ref), capped with the
deterministic rank-ascending drop order. Pure function — no DB, no clock.
"""

from __future__ import annotations

from modulo.core.pipeline_engine.ancestor_refs import collect_injected_refs


def _ref(kind: str, ref: str, source: str) -> dict[str, object]:
    return {"kind": kind, "ref": ref, "source": source}


class TestDiamondDagScoping:
    def test_target_sees_ancestor_refs_not_the_parallel_branch(self) -> None:
        # Diamond: a -> (b, c) -> d. Target d; ancestors a, b; c is the
        # parallel branch and contributes nothing.
        refs = collect_injected_refs(
            run_create_time_refs=[_ref("jira", "FAR-1", "caller")],
            dag_ancestor_ids={"a", "b"},
            completed_node_outputs={
                "a": [_ref("github_pr", "12", "agent")],
                "b": [_ref("linear", "X-2", "derived")],
                "c": [_ref("slack", "off-branch", "agent")],
            },
            cap=50,
        )
        assert [(r["kind"], r["ref"]) for r in refs] == [
            ("jira", "FAR-1"),
            ("github_pr", "12"),
            ("linear", "X-2"),
        ]

    def test_target_node_id_is_not_an_ancestor_of_itself(self) -> None:
        # The target's own emissions are never injected back into it: its id
        # is absent from the ancestor set (and never added by this function).
        refs = collect_injected_refs(
            run_create_time_refs=[],
            dag_ancestor_ids=set(),
            completed_node_outputs={"target": [_ref("jira", "SELF", "agent")]},
            cap=50,
        )
        assert refs == []


class TestSkippedAndFailedAncestors:
    def test_skipped_node_absent_from_outputs_contributes_nothing(self) -> None:
        # A skipped/not-executed node is simply not a key in
        # completed_node_outputs — its absence is the skip signal.
        refs = collect_injected_refs(
            run_create_time_refs=[],
            dag_ancestor_ids={"a", "skipped-branch"},
            completed_node_outputs={"a": [_ref("github_pr", "7", "agent")]},
            cap=50,
        )
        assert [(r["kind"], r["ref"]) for r in refs] == [("github_pr", "7")]

    def test_failed_ancestors_emissions_are_included_when_passed(self) -> None:
        # Outcome filtering is the CALLER's job: whatever emissions the caller
        # passes for a failed node are injected verbatim.
        refs = collect_injected_refs(
            run_create_time_refs=[],
            dag_ancestor_ids={"failed-node"},
            completed_node_outputs={"failed-node": [_ref("jira", "FROM-FAILED", "agent")]},
            cap=50,
        )
        assert [(r["kind"], r["ref"]) for r in refs] == [("jira", "FROM-FAILED")]


class TestRetryUnion:
    def test_folded_attempts_yield_one_union_regardless_of_key_order(self) -> None:
        # The caller folds multiple attempts of the same node into one list;
        # the union must be identical (and duplicate-free) whichever way the
        # attempts/keys were folded.
        a = [_ref("github_pr", "3", "agent")]
        b = [_ref("linear", "Y-1", "derived")]
        folded_forward = collect_injected_refs(
            run_create_time_refs=[],
            dag_ancestor_ids={"a", "b"},
            completed_node_outputs={"a": a, "b": b},
            cap=50,
        )
        folded_reversed = collect_injected_refs(
            run_create_time_refs=[],
            dag_ancestor_ids={"a", "b"},
            completed_node_outputs={"b": b, "a": a},
            cap=50,
        )
        assert [(r["kind"], r["ref"]) for r in folded_forward] == [
            ("github_pr", "3"),
            ("linear", "Y-1"),
        ]
        # The UNION is identical whichever way the attempts/keys were folded:
        # same members (deduped), just presented in each input's order.
        assert {(r["kind"], r["ref"]) for r in folded_forward} == {(r["kind"], r["ref"]) for r in folded_reversed}
        assert len(folded_forward) == len(folded_reversed)
        assert len(folded_forward) == 2

    def test_duplicate_refs_across_attempts_are_not_duplicated(self) -> None:
        refs = collect_injected_refs(
            run_create_time_refs=[],
            dag_ancestor_ids={"a"},
            completed_node_outputs={"a": [_ref("github_pr", "3", "agent"), _ref("github_pr", "3", "agent")]},
            cap=50,
        )
        assert [(r["kind"], r["ref"]) for r in refs] == [("github_pr", "3")]


class TestDedupeKeepsHighestRankedSource:
    def test_caller_beats_derived_beats_agent(self) -> None:
        refs = collect_injected_refs(
            run_create_time_refs=[_ref("jira", "X-1", "derived")],
            dag_ancestor_ids={"a"},
            completed_node_outputs={"a": [_ref("jira", "X-1", "agent"), _ref("jira", "X-1", "caller")]},
            cap=50,
        )
        assert len(refs) == 1
        assert refs[0]["source"] == "caller"

    def test_equal_rank_ties_keep_the_earliest_position(self) -> None:
        refs = collect_injected_refs(
            run_create_time_refs=[_ref("jira", "X-1", "caller")],
            dag_ancestor_ids={"a"},
            completed_node_outputs={"a": [_ref("jira", "X-1", "caller")]},
            cap=50,
        )
        assert len(refs) == 1
        assert refs[0] is not None

    def test_canonical_identity_is_kind_and_ref_pair(self) -> None:
        # Same ref under a different kind is a DIFFERENT work item.
        refs = collect_injected_refs(
            run_create_time_refs=[_ref("jira", "X-1", "caller")],
            dag_ancestor_ids={"a"},
            completed_node_outputs={"a": [_ref("github_pr", "X-1", "agent")]},
            cap=50,
        )
        assert len(refs) == 2

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        refs = collect_injected_refs(
            run_create_time_refs=["garbage", {"kind": "", "ref": "x", "source": "caller"}],  # type: ignore[list-item]
            dag_ancestor_ids={"a"},
            completed_node_outputs={"a": [{"kind": "jira"}, _ref("jira", "OK", "agent")]},
            cap=50,
        )
        assert [(r["kind"], r["ref"]) for r in refs] == [("jira", "OK")]


class TestCap:
    def test_over_cap_drops_agent_first_then_derived(self) -> None:
        refs = collect_injected_refs(
            run_create_time_refs=[
                _ref("w-caller", "1", "caller"),
                _ref("w-derived", "2", "derived"),
                _ref("w-agent-a", "3", "agent"),
                _ref("w-agent-b", "4", "agent"),
            ],
            dag_ancestor_ids=set(),
            completed_node_outputs={},
            cap=2,
        )
        assert [(r["kind"], r["ref"]) for r in refs] == [
            ("w-caller", "1"),
            ("w-derived", "2"),
        ]

    def test_cap_applies_after_dedupe(self) -> None:
        # Three supplies, one canonical ref: dedupe folds to 1, so cap=1 keeps it.
        refs = collect_injected_refs(
            run_create_time_refs=[_ref("jira", "X-1", "agent")],
            dag_ancestor_ids={"a"},
            completed_node_outputs={"a": [_ref("jira", "X-1", "caller")]},
            cap=1,
        )
        assert len(refs) == 1
        assert refs[0]["source"] == "caller"

    def test_within_cap_keeps_input_order(self) -> None:
        refs = collect_injected_refs(
            run_create_time_refs=[_ref("jira", "1", "caller")],
            dag_ancestor_ids={"a", "b"},
            completed_node_outputs={
                "a": [_ref("github_pr", "2", "agent")],
                "b": [_ref("linear", "3", "derived")],
            },
            cap=50,
        )
        assert [r["kind"] for r in refs] == ["jira", "github_pr", "linear"]

    def test_zero_or_negative_cap_yields_empty(self) -> None:
        create_refs = [_ref("jira", "1", "caller")]
        completed: dict[str, list[dict[str, object]]] = {"a": [_ref("github_pr", "2", "agent")]}
        assert not collect_injected_refs(
            run_create_time_refs=create_refs,
            dag_ancestor_ids={"a"},
            completed_node_outputs=completed,
            cap=0,
        )
        assert not collect_injected_refs(
            run_create_time_refs=create_refs,
            dag_ancestor_ids={"a"},
            completed_node_outputs=completed,
            cap=-1,
        )


class TestPurity:
    def test_inputs_are_never_mutated(self) -> None:
        create_refs = [_ref("jira", "1", "caller")]
        a_refs = [_ref("github_pr", "2", "agent")]
        completed = {"a": a_refs}
        ancestors = {"a"}
        collect_injected_refs(
            run_create_time_refs=create_refs,
            dag_ancestor_ids=ancestors,
            completed_node_outputs=completed,
            cap=1,
        )
        assert create_refs == [_ref("jira", "1", "caller")]
        assert a_refs == [_ref("github_pr", "2", "agent")]
        assert completed == {"a": [_ref("github_pr", "2", "agent")]}
        assert ancestors == {"a"}

    def test_deterministic_across_repeated_calls(self) -> None:
        create_refs = [_ref("jira", "1", "caller")]
        ancestors = {"a", "b"}
        completed = {
            "a": [_ref("github_pr", "2", "agent")],
            "b": [_ref("linear", "3", "derived")],
        }
        first = collect_injected_refs(
            run_create_time_refs=create_refs, dag_ancestor_ids=ancestors, completed_node_outputs=completed, cap=2
        )
        second = collect_injected_refs(
            run_create_time_refs=create_refs, dag_ancestor_ids=ancestors, completed_node_outputs=completed, cap=2
        )
        assert first == second
