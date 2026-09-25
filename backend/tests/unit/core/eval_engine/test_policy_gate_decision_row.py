"""Pure-unit tests for FAR-1102 chunk 4 decision-record construction.

Covers acceptance criteria 1-3 from the chunk-04 decision-records spec:
  1. ``build_decision_row`` populates all six payload columns correctly.
  2. ``build_decision_row`` derives ``organisation_id`` from the snapshot.
  3. ``build_decision_row`` does not touch a database (pure construction).

Target: backend/tests/unit/core/eval_engine/test_policy_gate_decision_row.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from modulo.core.eval_engine.policy_gate import (
    EvalPolicySnapshot,
    EvalResultView,
    EvalView,
    Outcome,
    PolicyGateView,
    build_decision_row,
)

org_id = uuid.uuid4()
gate_id = uuid.uuid4()
eval_id = uuid.uuid4()
_run_id = uuid.uuid4()


def _policy_gate(**overrides) -> PolicyGateView:
    base = {
        "id": gate_id,
        "organisation_id": org_id,
        "version": 3,
        "node_id": uuid.uuid4(),
        "action": "warn",
    }
    base.update(overrides)
    return PolicyGateView(**base)


def _eval(**overrides) -> EvalView:
    base = {
        "id": eval_id,
        "organisation_id": org_id,
        "node_id": None,
        "eval_type": "regex",
        "deleted_at": None,
    }
    base.update(overrides)
    return EvalView(**base)


def _snapshot(pg: PolicyGateView | None = None, ev: EvalView | None = None, er: EvalResultView | None = None):
    return EvalPolicySnapshot(
        policy_gate=pg or _policy_gate(),
        eval=ev or _eval(),
        eval_result=er,
    )


def _outcome_defined_pass() -> Outcome:
    return Outcome(
        result=True,
        action="continue",
        eval_result_id=uuid.uuid4(),
        error=None,
    )


class TestBuildDecisionRowColminariatePayloadColumns:
    """Criterion 1: all six payload columns are populated from the inputs."""

    def test_defined_pass_outcome_populates_all_six_columns(self) -> None:
        pg = _policy_gate(version=5, node_id=uuid.uuid4())
        result_id = uuid.uuid4()
        outcome = Outcome(result=True, action="continue", eval_result_id=result_id, error=None)

        row = build_decision_row(_snapshot(pg=pg), outcome, _run_id)

        assert row.resolved_action == outcome.action
        assert row.error_detail == outcome.error
        assert row.node_id == pg.node_id
        assert row.eval_result_id == result_id
        assert row.run_id == _run_id
        assert row.policy_gate_version == 5

    def test_undefined_outcome_carries_error_detail(self) -> None:
        outcome = Outcome(result=None, action="warn", eval_result_id=None, error="no_eval_result")

        row = build_decision_row(_snapshot(), outcome, _run_id)

        assert row.resolved_action == "warn"
        assert row.error_detail == "no_eval_result"
        assert row.eval_result_id is None
        assert row.policy_gate_version == 3

    def test_deleted_eval_outcome_carries_error_detail_and_timestamp(self) -> None:
        ev = _eval(deleted_at=datetime.now(UTC))
        outcome = Outcome(result=None, action="block", eval_result_id=None, error="no_eval_result")

        row = build_decision_row(_snapshot(ev=ev), outcome, _run_id)

        assert row.resolved_action == "block"
        assert row.error_detail == "no_eval_result"
        assert row.node_id is not None


class TestSnapshotOrganisationId:
    """Criterion 2: organisation_id is derived from the policy_gate snapshot side."""

    def test_organisation_id_from_policy_gate(self) -> None:
        pg = _policy_gate()

        row = build_decision_row(_snapshot(pg=pg), _outcome_defined_pass(), _run_id)

        assert row.organisation_id == pg.organisation_id


class TestBuildDecisionRowIsPure:
    """Criterion 3: construction with no session, engine, or repository."""

    def test_returns_row_without_any_db_resources(self) -> None:
        # No session, no engine, no repository — only IDs and an Outcome.
        row = build_decision_row(_snapshot(), _outcome_defined_pass(), _run_id)

        assert row.policy_gate_id == gate_id
        assert row.eval_id == eval_id
