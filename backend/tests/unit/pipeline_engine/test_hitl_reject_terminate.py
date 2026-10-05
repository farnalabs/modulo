"""FAR-1487: a HITL rejection with no reject route ENDS the run ``rejected``.

Covers the pure/unit seams of the change:

* the single disposition resolver (``route`` / ``terminate`` / ``proceed``) and
  its precedence (route > correction_target > explicit proceed > terminate),
* the compiled gate wiring (a conditional edge whose router returns LangGraph
  ``END`` on a STAMPED rejection, reusing the FAR-541 gate-identity check),
* the gate artifact carrying the compile-time disposition,
* the executor finalize trap (``complete`` -> ``rejected`` keyed on the gate
  artifact AND the committed ``hitl_claims`` row, with the supersede label),
* the reviewer briefing stating the reject consequence in all three states,
* classification / error-code / vocabulary reconciliation.

The DB-backed finalize + CHECK-constraint paths run in
``tests/integration/test_hitl_resume_roundtrip.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END

from modulo.core.hitl_manager.gate_coalescing import SUPERSEDE_REASON
from modulo.core.pipeline_engine import executor as executor_mod
from modulo.core.pipeline_engine.classify import (
    REASON_HITL_REJECTED,
    REASON_HITL_SUPERSEDED,
    RunClassificationValue,
    classify_run,
)
from modulo.core.pipeline_engine.error_codes import ERROR_CODE_REGISTRY
from modulo.core.pipeline_engine.graph_cache import (
    _add_hitl_review_edge,
    _make_gate_terminate_router,
    build_graph_from_json,
)
from modulo.core.pipeline_engine.hitl_context import (
    REJECT_DISPOSITION_PROCEED,
    REJECT_DISPOSITION_ROUTE,
    REJECT_DISPOSITION_TERMINATE,
    _resolve_consequences,
    resolve_reject_disposition,
)
from modulo.core.pipeline_engine.node_runner import _hitl_review_approve_reject_result
from modulo.db.crud.run import RUN_STATUS_WHITELIST
from modulo.db.models.run import TERMINAL_STATUSES

_REVIEW_ID = "hitl_review_src_tgt"


# ---------------------------------------------------------------------------
# Disposition resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "has_route", "expected"),
    [
        ({}, False, REJECT_DISPOSITION_TERMINATE),
        ({"on_reject": "terminate"}, False, REJECT_DISPOSITION_TERMINATE),
        ({"on_reject": "proceed"}, False, REJECT_DISPOSITION_PROCEED),
        # correction_target WINS over terminate.
        ({"correction_target": "node-c"}, False, REJECT_DISPOSITION_PROCEED),
        ({"correction_target": "node-c", "on_reject": "terminate"}, False, REJECT_DISPOSITION_PROCEED),
        # A reject destination always routes, even with an explicit proceed.
        ({}, True, REJECT_DISPOSITION_ROUTE),
        ({"on_reject": "proceed"}, True, REJECT_DISPOSITION_ROUTE),
        ({"correction_target": "node-c"}, True, REJECT_DISPOSITION_ROUTE),
    ],
    ids=[
        "empty-no-route-terminates",
        "explicit-terminate-no-route-terminates",
        "explicit-proceed-no-route-proceeds",
        "correction-target-no-route-proceeds",
        "correction-target-beats-terminate-no-route-proceeds",
        "empty-with-route-routes",
        "explicit-proceed-with-route-routes",
        "correction-target-with-route-routes",
    ],
)
def test_resolve_reject_disposition_precedence(config: dict[str, Any], has_route: bool, expected: str) -> None:
    assert resolve_reject_disposition(config, has_reject_route=has_route) == expected


def test_resolve_reject_disposition_tolerates_non_dict_config() -> None:
    assert resolve_reject_disposition(None, has_reject_route=False) == REJECT_DISPOSITION_TERMINATE


# ---------------------------------------------------------------------------
# Terminate router (FAR-541 stamp check reuse)
# ---------------------------------------------------------------------------


def _decision(action: str, review_id: str = _REVIEW_ID) -> dict[str, Any]:
    return {"_hitl_decision": {"action": action, "review_id": review_id}}


def test_terminate_router_ends_on_stamped_rejection() -> None:
    router = _make_gate_terminate_router("tgt", review_id=_REVIEW_ID)
    assert router(_decision("rejected")) == END


@pytest.mark.parametrize(
    "state",
    [
        _decision("approved"),
        _decision("rejected", review_id="hitl_review_other_gate"),  # stale/foreign stamp (FAR-541)
        {"_hitl_decision": {"action": "rejected"}},  # unstamped
        {"_hitl_decision": "rejected"},  # malformed
        {},  # gate skipped, no decision at all
    ],
    ids=["approved", "foreign-stamp", "unstamped", "malformed", "no-decision"],
)
def test_terminate_router_continues_unless_stamped_rejection(state: dict[str, Any]) -> None:
    router = _make_gate_terminate_router("tgt", review_id=_REVIEW_ID)
    assert router(state) == "tgt"


# ---------------------------------------------------------------------------
# Compiled wiring
# ---------------------------------------------------------------------------


class _RecordingGraph:
    def __init__(self) -> None:
        self.nodes: list[str] = []
        self.plain_edges: list[tuple[str, str]] = []
        self.conditional: dict[str, Any] = {}

    def add_node(self, name: str, _fn: Any) -> None:
        self.nodes.append(name)

    def add_edge(self, a: str, b: str) -> None:
        self.plain_edges.append((a, b))

    def add_conditional_edges(self, source: str, router: Any) -> None:
        self.conditional[source] = router


def _compile_gate(config: dict[str, Any], reject_targets: dict[str, str] | None = None) -> tuple[_RecordingGraph, dict]:
    graph = _RecordingGraph()
    hitl_config = dict(config)
    _add_hitl_review_edge(
        graph,  # type: ignore[arg-type]
        "src",
        "tgt",
        hitl_config,
        target_ids=set(),
        gate_node_ids=set(),
        reject_targets_by_source=reject_targets or {},
        eval_definitions_by_node=None,
        session_factory=None,
        org_id=None,
        node_type_map={},
    )
    return graph, hitl_config


def test_gate_without_reject_route_compiles_to_terminate_conditional_edge() -> None:
    graph, cfg = _compile_gate({"label": "Review"})
    # The gate hangs off a CONDITIONAL edge (END branch) - no plain gate->target edge.
    assert list(graph.conditional) == [_REVIEW_ID]
    assert (_REVIEW_ID, "tgt") not in graph.plain_edges
    assert graph.conditional[_REVIEW_ID](_decision("rejected")) == END
    assert graph.conditional[_REVIEW_ID](_decision("approved")) == "tgt"
    assert cfg["reject_disposition"] == REJECT_DISPOSITION_TERMINATE


def test_gate_with_explicit_proceed_keeps_plain_edge() -> None:
    graph, cfg = _compile_gate({"label": "Review", "on_reject": "proceed"})
    assert not graph.conditional
    assert (_REVIEW_ID, "tgt") in graph.plain_edges
    assert cfg["reject_disposition"] == REJECT_DISPOSITION_PROCEED


def test_gate_with_correction_target_wins_over_terminate() -> None:
    graph, cfg = _compile_gate({"label": "Review", "correction_target": "node-c"})
    assert not graph.conditional
    assert (_REVIEW_ID, "tgt") in graph.plain_edges
    assert cfg["reject_disposition"] == REJECT_DISPOSITION_PROCEED


@pytest.mark.parametrize(
    ("config", "reject_targets"),
    [
        ({"reject_target": "fixer"}, {}),
        ({}, {"src": "fixer"}),
        ({"on_reject": "proceed", "reject_target": "fixer"}, {}),
    ],
    ids=["config-route", "edge-route", "route-beats-proceed"],
)
def test_gate_with_reject_route_still_routes(config: dict[str, Any], reject_targets: dict[str, str]) -> None:
    graph, cfg = _compile_gate(config, reject_targets)
    assert graph.conditional[_REVIEW_ID](_decision("rejected")) == "fixer"
    assert graph.conditional[_REVIEW_ID](_decision("approved")) == "tgt"
    assert cfg["reject_disposition"] == REJECT_DISPOSITION_ROUTE


def test_full_graph_with_terminating_gate_compiles() -> None:
    graph: dict[str, Any] = {
        "nodes": [{"id": "source", "role": None}, {"id": "target", "role": None}],
        "edges": [
            {
                "source": "source",
                "target": "target",
                "type": "normal",
                "hitl_review_config": {"label": "Review", "description": "Gate", "claim_expiry_minutes": 60},
            }
        ],
    }
    assert build_graph_from_json(graph) is not None


# ---------------------------------------------------------------------------
# Gate artifact carries the disposition
# ---------------------------------------------------------------------------


def test_rejected_artifact_carries_reject_disposition() -> None:
    result = _hitl_review_approve_reject_result(_REVIEW_ID, {"action": "rejected"}, True, "terminate")
    artifact = result["artifacts"][0]
    assert artifact["result"] == "rejected"
    assert artifact["reject_disposition"] == "terminate"


def test_approved_artifact_never_carries_reject_disposition() -> None:
    result = _hitl_review_approve_reject_result(_REVIEW_ID, {"action": "approved"}, False, "terminate")
    assert "reject_disposition" not in result["artifacts"][0]


# ---------------------------------------------------------------------------
# Executor finalize trap
# ---------------------------------------------------------------------------


def _output(result: str, *, node_id: str = _REVIEW_ID, disposition: str | None = "terminate") -> dict[str, Any]:
    artifact: dict[str, Any] = {"node_id": node_id, "status": "interrupted", "result": result}
    if disposition is not None:
        artifact["reject_disposition"] = disposition
    return {"artifacts": [artifact]}


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_output("rejected"), True),
        (_output("approved"), False),
        (_output("rejected", disposition="route"), False),
        (_output("rejected", disposition="proceed"), False),
        (_output("rejected", disposition=None), False),
        (_output("rejected", node_id="some_other_gate"), False),
        ({"artifacts": "nope"}, False),
        (None, False),
    ],
    ids=["terminate", "approved", "route", "proceed", "unstamped", "foreign-gate", "malformed", "none"],
)
def test_is_terminating_rejection_output(output: Any, expected: bool) -> None:
    assert executor_mod._is_terminating_rejection_output(_REVIEW_ID, output) is expected


def _claim(*, reason: str | None, decided_by: Any = None, account_id: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        decision="rejected",
        decision_payload={"action": "rejected", "reason": reason} if reason is not None else None,
        decided_by=decided_by,
        account_id=account_id,
    )


def test_supersede_rejection_requires_system_reason_and_no_account() -> None:
    assert executor_mod._is_supersede_rejection(_claim(reason=SUPERSEDE_REASON)) is True
    # A human typing the supersede reason must never spoof the label.
    assert executor_mod._is_supersede_rejection(_claim(reason=SUPERSEDE_REASON, decided_by="acct")) is False
    assert executor_mod._is_supersede_rejection(_claim(reason=SUPERSEDE_REASON, account_id="acct")) is False
    assert executor_mod._is_supersede_rejection(_claim(reason="wrong answer")) is False
    assert executor_mod._is_supersede_rejection(_claim(reason=None)) is False


def _executor_with_claims(claims: list[Any], gates: set[str]) -> executor_mod.PipelineExecutor:
    executor = executor_mod.PipelineExecutor(MagicMock())
    executor._terminating_rejected_gates = set(gates)
    scalars = MagicMock()
    scalars.all.return_value = claims
    result = MagicMock()
    result.scalars.return_value = scalars
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _begin() -> Any:
        yield None

    session.begin = _begin

    @asynccontextmanager
    async def _factory() -> Any:
        yield session

    executor._session_factory = _factory  # type: ignore[assignment]
    return executor


async def _downgrade(executor: executor_mod.PipelineExecutor) -> tuple[str, str | None, str | None]:
    with (
        patch.object(executor_mod, "set_rls_org", new=AsyncMock()),
        patch.object(executor_mod, "set_rls_execution_context", new=AsyncMock()),
    ):
        import uuid

        return await executor._downgrade_hitl_terminate_rejection(
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            final_status="complete",
            error_code=None,
            error_detail=None,
        )


async def test_finalize_downgrades_complete_to_rejected_on_committed_human_rejection() -> None:
    executor = _executor_with_claims([_claim(reason="wrong answer", decided_by="acct")], {_REVIEW_ID})
    status, code, detail = await _downgrade(executor)
    assert status == "rejected"
    assert code == "hitl.rejected"
    assert detail is not None


async def test_finalize_labels_supersede_rejection_as_superseded_not_human_reject() -> None:
    executor = _executor_with_claims([_claim(reason=SUPERSEDE_REASON)], {_REVIEW_ID})
    status, code, _ = await _downgrade(executor)
    assert status == "rejected"
    assert code == "hitl.superseded"


async def test_finalize_prefers_human_label_when_any_gate_was_human_rejected() -> None:
    claims = [_claim(reason=SUPERSEDE_REASON), _claim(reason="no", decided_by="acct")]
    executor = _executor_with_claims(claims, {"g1", "g2"})
    _, code, _ = await _downgrade(executor)
    assert code == "hitl.rejected"


async def test_finalize_keeps_complete_without_committed_rejected_claim() -> None:
    """The gate artifact alone (the graph's/agent's echo) is NEVER enough: with
    no committed ``hitl_claims.decision='rejected'`` row the run stays complete."""
    executor = _executor_with_claims([], {_REVIEW_ID})
    assert (await _downgrade(executor))[0] == "complete"


async def test_finalize_keeps_complete_without_terminating_gate_artifact() -> None:
    """A committed rejected row alone is not enough either (route / proceed
    gates reject without ending the run): no latched terminate artifact -> complete."""
    executor = _executor_with_claims([_claim(reason="no", decided_by="acct")], set())
    assert (await _downgrade(executor))[0] == "complete"


async def test_finalize_fails_safe_when_claim_lookup_raises() -> None:
    executor = _executor_with_claims([], {_REVIEW_ID})
    executor._session_factory = MagicMock(side_effect=RuntimeError("db down"))  # type: ignore[assignment]
    assert (await _downgrade(executor))[0] == "complete"


async def test_finalize_reraises_cancelled_error_from_claim_lookup() -> None:
    """A cancellation during the claim lookup must propagate untouched - it is
    the worker shutting down, not a lookup failure to fail-safe around."""
    executor = _executor_with_claims([], {_REVIEW_ID})
    executor._session_factory = MagicMock(side_effect=asyncio.CancelledError())  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        await _downgrade(executor)


# ---------------------------------------------------------------------------
# Synthetic HITL gate events latch the terminate-rejection in stream state
# ---------------------------------------------------------------------------


async def test_chain_end_latches_terminating_rejection_for_synthetic_gate() -> None:
    """A gate node is synthetic (absent from ``node_ids``); its stamped
    terminate-rejection artifact must still be latched so finalize can
    downgrade the run."""
    executor = executor_mod.PipelineExecutor(MagicMock())
    state = executor_mod._StreamState()
    ctx = SimpleNamespace(node_ids=set())
    event = {"name": _REVIEW_ID, "data": {"output": _output("rejected")}}
    await executor._handle_chain_end_event(state=state, ctx=ctx, lg_event=event)
    assert state.terminating_rejected_gates == {_REVIEW_ID}


async def test_chain_end_ignores_non_terminating_synthetic_output() -> None:
    executor = executor_mod.PipelineExecutor(MagicMock())
    state = executor_mod._StreamState()
    ctx = SimpleNamespace(node_ids=set())
    event = {"name": _REVIEW_ID, "data": {"output": _output("approved")}}
    await executor._handle_chain_end_event(state=state, ctx=ctx, lg_event=event)
    assert state.terminating_rejected_gates == set()


# ---------------------------------------------------------------------------
# Reviewer briefing states the consequence in all three dispositions
# ---------------------------------------------------------------------------

_SRC, _TGT, _FIX = "src-node", "tgt-node", "fixer-node"
_BRIEFING_REVIEW_ID = f"hitl_review_{_SRC}_{_TGT}"


def _briefing_graph() -> dict[str, Any]:
    return {
        "nodes": [
            {"id": _SRC, "label": "Writer"},
            {"id": _TGT, "label": "Poster"},
            {"id": _FIX, "label": "Fixer"},
        ],
        "edges": [{"source": _SRC, "target": _TGT, "type": "normal"}],
    }


def test_briefing_states_terminate_by_default() -> None:
    reject = _resolve_consequences(_briefing_graph(), _BRIEFING_REVIEW_ID, {"label": "Gate"}, _SRC)["reject"]  # type: ignore[index]
    assert reject == {"disposition": REJECT_DISPOSITION_TERMINATE}


def test_briefing_states_proceed_when_explicit() -> None:
    config = {"label": "Gate", "on_reject": "proceed"}
    reject = _resolve_consequences(_briefing_graph(), _BRIEFING_REVIEW_ID, config, _SRC)["reject"]  # type: ignore[index]
    assert reject == {"disposition": REJECT_DISPOSITION_PROCEED}


def test_briefing_states_route_with_target_when_reject_target_set() -> None:
    config = {"label": "Gate", "reject_target": _FIX}
    reject = _resolve_consequences(_briefing_graph(), _BRIEFING_REVIEW_ID, config, _SRC)["reject"]  # type: ignore[index]
    assert reject == {"disposition": REJECT_DISPOSITION_ROUTE, "node_id": _FIX, "label": "Fixer"}


def test_briefing_correction_target_wins_over_terminate() -> None:
    config = {"label": "Gate", "correction_target": _FIX}
    reject = _resolve_consequences(_briefing_graph(), _BRIEFING_REVIEW_ID, config, _SRC)["reject"]  # type: ignore[index]
    assert reject["disposition"] == REJECT_DISPOSITION_PROCEED


# ---------------------------------------------------------------------------
# Vocabulary / classification / error-code reconciliation
# ---------------------------------------------------------------------------


def test_rejected_is_a_terminal_whitelisted_status() -> None:
    assert "rejected" in TERMINAL_STATUSES
    assert "rejected" in RUN_STATUS_WHITELIST


@pytest.mark.parametrize(
    ("error_code", "reason"),
    [("hitl.rejected", REASON_HITL_REJECTED), ("hitl.superseded", REASON_HITL_SUPERSEDED)],
)
def test_rejected_runs_classify_excluded_with_their_own_reason(error_code: str, reason: str) -> None:
    result = classify_run("rejected", error_code)
    assert result.value == RunClassificationValue.excluded
    assert result.reason == reason


def test_hitl_reject_error_codes_are_registered_silent_and_non_retryable() -> None:
    for code in ("hitl.rejected", "hitl.superseded"):
        spec = ERROR_CODE_REGISTRY[code]
        assert spec.error_class == "hitl"
        assert spec.retryable is False
        assert spec.alert_severity is None
