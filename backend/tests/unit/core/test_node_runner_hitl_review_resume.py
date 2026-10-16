"""Unit tests for the HITL gate resume seam (``_hitl_review_resume_result``).

FAR-541: the gate node must fail closed on an action-less decision AND on a
decision not stamped for THIS gate. Only recognized actions stamped with the
gate's own id resume — ``approved`` (the approve-with-modification API
submits ``approved`` plus a ``modified_output`` member, so it is covered by
``approved``), ``rejected``, and ``deliver_manual``. Anything else (an empty
``{}``, an unknown action value, a non-dict decision, a decision stamped for
a DIFFERENT gate) is ignored — the gate falls through to the
condition/eval/autonomy path and re-interrupts rather than treating the
malformed or foreign decision as an approval.

All DB-adjacent collaborators are inert here: the approve/reject path calls
``_dispatch_reject_correction_best_effort`` with ``session_factory=None`` /
``org_id=None``, which no-ops.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.pipeline_engine.node_runner as nr

_REVIEW_ID = "gate_a_b"
_FOREIGN_REVIEW_ID = "gate_c_d"


def _resume(decision: Any) -> tuple[bool, dict[str, Any] | None]:
    return nr._hitl_review_resume_result(
        decision,
        _REVIEW_ID,
        state={},
        hitl_review_config={},
        session_factory=None,
        org_id=None,
    )


# --- Fail-closed: malformed decisions must never resume (FAR-541) ---------


async def test_none_decision_does_not_resume() -> None:
    """First invocation (no decision in state) -> fall through, not resumed."""
    resumed, result = await _resume(None)
    assert resumed is False
    assert result is None


async def test_empty_dict_decision_fails_closed() -> None:
    """THE FIX (FAR-541): an empty ``{}`` decision (injected by an
    empty-resume dispatch) is NOT an approval — the gate fails closed."""
    resumed, result = await _resume({})
    assert resumed is False
    assert result is None


async def test_unknown_action_fails_closed() -> None:
    """An unrecognized action value is ignored, never treated as an approval."""
    resumed, result = await _resume({"action": "weird", "review_id": _REVIEW_ID})
    assert resumed is False
    assert result is None


async def test_missing_action_fails_closed() -> None:
    """A dict without an ``action`` member carries no human verdict."""
    resumed, result = await _resume({"notes": "just notes", "review_id": _REVIEW_ID})
    assert resumed is False
    assert result is None


async def test_non_dict_decision_fails_closed() -> None:
    """A non-dict decision (e.g. a bare string) is not a human verdict — the
    old code treated any non-None non-deliver_manual decision as approved."""
    resumed, result = await _resume("approved")
    assert resumed is False
    assert result is None


async def test_malformed_decision_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The fail-closed branch emits ``hitl_review.malformed_decision_ignored``
    with the gate id and the decision's type/action (never payload content)."""
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await _resume({"action": "weird", "review_id": _REVIEW_ID, "secret_payload": "do-not-log"})
    records = [r for r in caplog.records if r.getMessage() == "hitl_review.malformed_decision_ignored"]
    assert len(records) == 1
    assert records[0].review_id == _REVIEW_ID
    assert records[0].decision_type == "dict"
    assert records[0].action == "weird"


# --- Fail-closed: foreign decisions must never resume (FAR-541 iteration 2) --


async def test_foreign_stamped_decision_does_not_resume() -> None:
    """THE C1 REGRESSION (FAR-541 iteration 2): a decision stamped for a
    DIFFERENT gate (decided at gate A, replayed at gate B by a stale-state
    resume) must NOT resolve gate B — decisions are per-RUN but consumers are
    per-gate, and ``_hitl_decision`` is never cleared from state."""
    resumed, result = await _resume({"action": "approved", "review_id": _FOREIGN_REVIEW_ID})
    assert resumed is False
    assert result is None


async def test_foreign_rejected_decision_does_not_resume() -> None:
    """A foreign REJECTION is equally inert — it must not kick gate B to its
    reject target nor dispatch a reject correction for a decision B never
    received."""
    resumed, result = await _resume({"action": "rejected", "review_id": _FOREIGN_REVIEW_ID, "reason": "gate A"})
    assert resumed is False
    assert result is None


async def test_unstamped_decision_does_not_resume() -> None:
    """A missing stamp fails closed: every live writer (API routes, MCP,
    dispatcher reconcile) stamps the gate id — an unstamped decision is not a
    verdict for this gate."""
    resumed, result = await _resume({"action": "approved"})
    assert resumed is False
    assert result is None


async def test_foreign_decision_logs_warning_without_payload(caplog: pytest.LogCaptureFixture) -> None:
    """``hitl_review.foreign_decision_ignored`` carries the gate id and the
    decision's stamped gate id ONLY — never payload content."""
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await _resume(
            {"action": "approved", "review_id": _FOREIGN_REVIEW_ID, "notes": "do-not-log-me", "output": {"x": 1}}
        )
    records = [r for r in caplog.records if r.getMessage() == "hitl_review.foreign_decision_ignored"]
    assert len(records) == 1
    assert records[0].review_id == _REVIEW_ID
    assert records[0].decision_review_id == _FOREIGN_REVIEW_ID
    assert "do-not-log-me" not in caplog.text
    assert "output" not in caplog.text


# --- Recognized actions resume ---------------------------------------------


async def test_approved_decision_resumes() -> None:
    resumed, result = await _resume({"action": "approved", "review_id": _REVIEW_ID})
    assert resumed is True
    assert result is not None
    artifact = result["artifacts"][0]
    assert artifact["node_id"] == _REVIEW_ID
    assert artifact["result"] == "approved"


async def test_approved_with_modification_resumes_with_output() -> None:
    """The approve-with-modification API submits ``approved`` plus
    ``modified_output`` — the modification flows into the run output."""
    modified = {"pr_title": "human edited"}
    resumed, result = await _resume({"action": "approved", "review_id": _REVIEW_ID, "modified_output": modified})
    assert resumed is True
    assert result is not None
    assert result["output"] == modified


async def test_rejected_decision_resumes() -> None:
    resumed, result = await _resume({"action": "rejected", "review_id": _REVIEW_ID, "reason": "not good enough"})
    assert resumed is True
    assert result is not None
    assert result["artifacts"][0]["result"] == "rejected"


async def test_deliver_manual_decision_resumes() -> None:
    manual_output = {"answer": 42}
    resumed, result = await _resume({"action": "deliver_manual", "review_id": _REVIEW_ID, "output": manual_output})
    assert resumed is True
    assert result is not None
    assert result["artifacts"][0]["result"] == "delivered_manual"
    assert result["output"] == manual_output


# --- M6: one human-decision vocabulary, no dead constant -------------------


def test_human_decision_vocabulary_has_a_single_source() -> None:
    """``_HUMAN_DECISION_ACTIONS`` (the resume path's membership check) must
    be exactly the closed ``HumanDecision.action`` literal the resolver
    consumes — the constant previously had NO call site while the resume path
    inlined the same set, so the two could drift. Pinned in lockstep here."""
    from typing import get_args, get_type_hints

    from modulo.core.eval_engine.policy_gate import HumanDecision

    resolver_vocabulary = set(get_args(get_type_hints(HumanDecision)["action"]))
    assert set(nr._HUMAN_DECISION_ACTIONS) == resolver_vocabulary
    assert resolver_vocabulary == {"approved", "rejected", "deliver_manual"}


# --- M8 (criterion 12): the grounding fail-open guard covers the WHOLE body -


async def test_grounding_constructor_failure_is_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    """Pre-M8 the lazy imports and ``HumanDecision(...)`` executed BEFORE the
    ``try``, so a failure there escaped ``resume`` after the human's decision
    had already committed — violating §3.4 / criterion 12. The whole
    post-decision body now sits inside the guard: the failure is logged with
    its structured event name and swallowed (only ``CancelledError``
    propagates).
    """
    import uuid

    from modulo.core.eval_engine import policy_gate as policy_gate_module
    from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

    dto = EvalDefDTO(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="human-gate-eval",
        eval_type="regex",
        config={},
        failure_behaviour="block",
        node_id="node-a",
        policy_gate_id=uuid.uuid4(),
        policy_gate_version=1,
        policy_gate_node_id=uuid.uuid4(),
        policy_gate_evaluation_mode="human",
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("resolver construction exploded")

    class _SessionNeverOpens:
        """``session_factory()`` must yield an async CM — never entered here."""

        async def __aenter__(self) -> object:
            raise AssertionError("the session must never open when construction fails")

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    def _session_factory() -> object:
        return _SessionNeverOpens()

    with (
        patch.object(policy_gate_module, "HumanDecision", _boom),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        # No pytest.raises: the point is that NOTHING escapes.
        await nr._ground_human_gate_evidence(
            {"action": "approved", "review_id": _REVIEW_ID},
            [dto],
            session_factory=_session_factory,
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            review_id=_REVIEW_ID,
        )

    failures = [r for r in caplog.records if r.getMessage() == "hitl_review.human_gate_evidence_grounding_failed"]
    assert failures, "the construction failure must be logged with its structured event name"
    assert failures[0].human_action == "approved"


async def test_grounding_cancelled_error_propagates() -> None:
    """``CancelledError`` is the one exception that must NOT be swallowed by
    the fail-open guard."""
    import asyncio
    import uuid

    from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

    dto = EvalDefDTO(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="human-gate-eval",
        eval_type="regex",
        config={},
        failure_behaviour="block",
        node_id="node-a",
        policy_gate_id=uuid.uuid4(),
        policy_gate_version=1,
        policy_gate_node_id=uuid.uuid4(),
        policy_gate_evaluation_mode="human",
    )

    class _CancelledSession:
        async def __aenter__(self) -> object:
            raise asyncio.CancelledError

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    def _session_factory() -> object:
        return _CancelledSession()

    with pytest.raises(asyncio.CancelledError):
        await nr._ground_human_gate_evidence(
            {"action": "approved", "review_id": _REVIEW_ID},
            [dto],
            session_factory=_session_factory,
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            review_id=_REVIEW_ID,
        )


# --- FAR-939 §3.4 grounding happy paths and per-DTO branches ----------------


def _grounding_dto(*, failure_behaviour: str = "block") -> Any:
    """A human-governed DTO as the resume path's DTO universe carries it."""
    from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

    return EvalDefDTO(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="human-gate-eval",
        eval_type="regex",
        config={},
        failure_behaviour=failure_behaviour,
        node_id="node-a",
        policy_gate_id=uuid.uuid4(),
        policy_gate_version=1,
        policy_gate_node_id=uuid.uuid4(),
        policy_gate_evaluation_mode="human",
    )


def _open_session() -> MagicMock:
    """A session whose ``begin()`` opens — the grounding write path."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _session_factory_for(session: MagicMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return MagicMock(side_effect=lambda: _ctx())


async def test_grounding_skips_when_session_org_or_run_is_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A grounding call that cannot open a DB context (no session_factory /
    org_id / run_id) must log the structured skip and return — never raise
    into the already-committed decision."""
    dto = _grounding_dto()

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await nr._ground_human_gate_evidence(
            {"action": "approved"},
            [dto],
            session_factory=None,
            org_id=None,
            run_id=None,
            review_id=_REVIEW_ID,
        )

    skips = [r for r in caplog.records if r.getMessage() == "hitl_review.human_gate_evidence_grounding_skipped"]
    assert len(skips) == 1
    assert skips[0].reason == "missing_session_org_or_run"
    assert skips[0].human_action == "approved"


async def test_grounding_appends_one_evidence_row_for_a_resolved_decision() -> None:
    """The happy path: an approved decision on a block-governed human eval
    grounds exactly ONE Evidence fact — ``gate_decision_<gate_id>`` on the
    run, value ``True`` (approved), produced by the policy gate."""
    dto = _grounding_dto(failure_behaviour="block")
    session = _open_session()
    captured: dict[str, Any] = {}

    def _append(_db: Any, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with (
        patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.eval_engine.evidence_layer.append_evidence", side_effect=_append),
    ):
        await nr._ground_human_gate_evidence(
            {"action": "approved", "decided_by": "user-1", "reason": "lgtm"},
            [dto],
            session_factory=_session_factory_for(session),
            org_id=org_id,
            run_id=run_id,
            review_id=_REVIEW_ID,
        )

    assert captured["key"] == f"gate_decision_{dto.policy_gate_id}"
    assert captured["value"] is True
    assert captured["subject_type"] == "run"
    assert captured["subject_id"] == str(run_id)
    assert captured["organisation_id"] == org_id
    assert captured["producer_type"] == "policy_gate"
    assert captured["producer_id"] == dto.policy_gate_id


async def test_grounding_logs_out_of_set_gate_action_and_still_grounds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live gate row whose ``failure_behaviour`` is outside the Outcome
    vocabulary (``escalate``, FAR-1694/0305) is LOUDLY logged and still
    grounded — the resolver fail-closes the action to ``block``, and a
    rejection must never lose its Evidence row."""
    dto = _grounding_dto(failure_behaviour="escalate")
    session = _open_session()
    append_mock = MagicMock()

    with (
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
        patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.eval_engine.evidence_layer.append_evidence", append_mock),
    ):
        await nr._ground_human_gate_evidence(
            {"action": "rejected"},
            [dto],
            session_factory=_session_factory_for(session),
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            review_id=_REVIEW_ID,
        )

    out_of_set = [r for r in caplog.records if r.getMessage() == "hitl_review.human_gate_action_out_of_set"]
    assert len(out_of_set) == 1
    assert out_of_set[0].action == "escalate"
    assert out_of_set[0].policy_gate_id == str(dto.policy_gate_id)
    append_mock.assert_called_once()
    assert append_mock.call_args.kwargs["value"] is False  # rejected → False


async def test_grounding_skips_an_unrecognised_human_action(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An action the resolver cannot resolve (``result=None``) is not a
    decision — nothing is grounded, and the unresolved case is logged."""
    dto = _grounding_dto()
    session = _open_session()
    append_mock = MagicMock()

    with (
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
        patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.eval_engine.evidence_layer.append_evidence", append_mock),
    ):
        await nr._ground_human_gate_evidence(
            {"action": "not-a-recognised-gate-action"},
            [dto],
            session_factory=_session_factory_for(session),
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            review_id=_REVIEW_ID,
        )

    unresolved = [r for r in caplog.records if r.getMessage() == "hitl_review.human_gate_evidence_unresolved_decision"]
    assert len(unresolved) == 1
    assert unresolved[0].human_action == "not-a-recognised-gate-action"
    append_mock.assert_not_called()
